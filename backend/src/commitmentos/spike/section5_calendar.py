"""Phase 0 Section 5 spike routes — Calendar watch delivery and the public webhook boundary.

Proves: channel-token-validated public webhook -> durable coalesced sync
request -> named source-sync Cloud Task -> bounded incremental events fetch
with an unpromoted candidate sync token.

The webhook is deliberately public at the IAM edge (Calendar push carries no
OIDC identity); every trust decision happens here, before any durable write.
Replaced by the real command stack in Phase 1.
"""

from __future__ import annotations

import hmac
import json
import time
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request, Response
from google.api_core import exceptions as gapi_exceptions
from google.auth.exceptions import RefreshError
from google.cloud import firestore, tasks_v2
from googleapiclient.discovery import build as build_google_api
from googleapiclient.errors import HttpError

from commitmentos.bootstrap.settings import Settings
from commitmentos.spike.section4_gmail import (
    _access_secret,
    _controlled_credentials,
    _firestore_client,
    _tasks_client,
)

EVENTS_PAGE_SIZE = 25
RATE_LIMIT_MAX_SIGNALS = 20
RATE_LIMIT_WINDOW_SECONDS = 60
VALID_RESOURCE_STATES = ("sync", "exists", "not_exists")

_signal_times: dict[str, deque[float]] = {}


@lru_cache(maxsize=1)
def _channel_secret(ref: str) -> str:
    return _access_secret(ref).strip()


def _rate_limited(channel_id: str) -> bool:
    now = time.monotonic()
    times = _signal_times.setdefault(channel_id, deque())
    while times and now - times[0] > RATE_LIMIT_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= RATE_LIMIT_MAX_SIGNALS:
        return True
    times.append(now)
    return False


def fetch_calendar_page(settings: Settings) -> dict:
    project = settings.google_cloud_project
    cursor_ref = (
        _firestore_client(project)
        .collection("sync_cursors")
        .document(f"calendar:{settings.controlled_user_id}")
    )
    request_ref = (
        _firestore_client(project)
        .collection("sync_requests")
        .document(f"calendar:{settings.controlled_user_id}")
    )
    snapshot = cursor_ref.get()
    if not snapshot.exists:
        raise HTTPException(
            status_code=409, detail="no published calendar sync token; register the watch first"
        )
    published_sync_token = snapshot.get("published_sync_token")

    credentials = _controlled_credentials(
        settings.oauth_client_secret_ref,
        settings.controlled_refresh_token_secret_ref,
    )
    service = build_google_api("calendar", "v3", credentials=credentials, cache_discovery=False)
    try:
        response = (
            service.events()
            .list(
                calendarId=settings.calendar_id,
                syncToken=published_sync_token,
                maxResults=EVENTS_PAGE_SIZE,
            )
            .execute()
        )
    except RefreshError:
        _controlled_credentials.cache_clear()
        request_ref.set(
            {
                "status": "reauth_required",
                "auth_error": "invalid_grant",
                "auth_failed_at": datetime.now(timezone.utc),
            },
            merge=True,
        )
        return {"status": "reauth_required", "detail": "credential refresh failed; dependent work stopped"}
    except HttpError as error:
        if error.resp.status == 410:
            request_ref.set(
                {
                    "status": "full_resync_required",
                    "observed_at": datetime.now(timezone.utc),
                },
                merge=True,
            )
            return {"status": "full_resync_required"}
        raise

    changed_events = response.get("items", [])
    candidate_sync_token = response.get("nextSyncToken")
    next_page_token_present = "nextPageToken" in response

    request_ref.set(
        {
            "status": "fetched",
            "fetched_at": datetime.now(timezone.utc),
            "published_sync_token_unchanged": published_sync_token,
            "candidate_sync_token_present": candidate_sync_token is not None,
            "changed_events_seen": len(changed_events),
            "next_page_token_present": next_page_token_present,
        },
        merge=True,
    )
    return {
        "status": "fetched",
        "changed_events": len(changed_events),
        "candidate_sync_token_present": candidate_sync_token is not None,
        "next_page_token_present": next_page_token_present,
    }


def build_calendar_webhook_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    project = settings.google_cloud_project

    def _channel_doc() -> firestore.DocumentReference:
        return (
            _firestore_client(project)
            .collection("calendar_channels")
            .document(settings.controlled_user_id)
        )

    def _request_ref() -> firestore.DocumentReference:
        return (
            _firestore_client(project)
            .collection("sync_requests")
            .document(f"calendar:{settings.controlled_user_id}")
        )

    @router.post(settings.calendar_webhook_path, status_code=204)
    async def calendar_webhook(request: Request) -> Response:
        body = await request.body()
        if body:
            raise HTTPException(status_code=400, detail="unexpected body")

        token = request.headers.get("X-Goog-Channel-Token")
        if not token:
            raise HTTPException(status_code=401, detail="missing channel token")
        if not hmac.compare_digest(
            token, _channel_secret(settings.calendar_channel_secret_ref)
        ):
            raise HTTPException(status_code=403, detail="invalid channel token")

        resource_state = request.headers.get("X-Goog-Resource-State", "")
        if resource_state not in VALID_RESOURCE_STATES:
            raise HTTPException(status_code=400, detail="invalid resource state")

        channel_id = request.headers.get("X-Goog-Channel-ID", "")
        if _rate_limited(channel_id):
            raise HTTPException(status_code=429, detail="rate limited")

        snapshot = _channel_doc().get()
        if not snapshot.exists:
            raise HTTPException(status_code=403, detail="no registered channel")
        channel = snapshot.to_dict()
        if channel_id not in {channel.get("channel_id"), channel.get("previous_channel_id")}:
            raise HTTPException(status_code=403, detail="unknown channel")
        if channel_id == channel.get("channel_id") and request.headers.get(
            "X-Goog-Resource-ID", ""
        ) != channel.get("resource_id"):
            raise HTTPException(status_code=403, detail="unknown resource")
        if channel.get("calendar_id") != settings.calendar_id:
            raise HTTPException(status_code=403, detail="unexpected calendar mapping")

        if resource_state == "sync":
            _channel_doc().set(
                {"sync_handshake_at": datetime.now(timezone.utc)}, merge=True
            )
            return Response(status_code=204)

        now = datetime.now(timezone.utc)
        message_number = request.headers.get("X-Goog-Message-Number", "0")
        client = _firestore_client(project)
        request_ref = _request_ref()
        transaction = client.transaction()

        @firestore.transactional
        def _coalesce(txn: firestore.Transaction) -> None:
            snapshot = request_ref.get(transaction=txn)
            existing = snapshot.to_dict() if snapshot.exists else {}
            txn.set(
                request_ref,
                {
                    "source": "calendar",
                    "user_id": settings.controlled_user_id,
                    "status": "pending",
                    "signal_count": firestore.Increment(1),
                    "last_message_number": int(message_number),
                    "updated_at": now,
                    "created_at": existing.get("created_at", now),
                },
                merge=True,
            )

        _coalesce(transaction)

        queue_path = _tasks_client().queue_path(
            project, settings.google_cloud_region, settings.source_sync_queue
        )
        task_body = json.dumps(
            {
                "schema": settings.task_schema_version,
                "source": "calendar",
                "user_id": settings.controlled_user_id,
                "message_number": int(message_number),
            }
        ).encode("utf-8")
        try:
            _tasks_client().create_task(
                parent=queue_path,
                task={
                    "name": f"{queue_path}/tasks/calsync-{settings.controlled_user_id}-{message_number}",
                    "http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "url": f"{str(settings.service_base_url).rstrip('/')}/internal/tasks/source-sync",
                        "headers": {"Content-Type": "application/json"},
                        "body": task_body,
                        "oidc_token": {
                            "service_account_email": settings.tasks_service_account,
                            "audience": settings.tasks_oidc_audience,
                        },
                    },
                },
            )
        except gapi_exceptions.AlreadyExists:
            pass  # replayed signal with the same message number converged
        return Response(status_code=204)

    return router
