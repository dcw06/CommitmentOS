"""Phase 0 Section 4 spike routes — Gmail watch delivery.

Proves: OIDC-validated Pub/Sub push -> durable coalesced sync request ->
named source-sync Cloud Task -> bounded history.list fetch with an
unpromoted candidate cursor.

These routes are replaced by the real command stack (ReceiveGmailSignal /
SynchronizeSource) in Phase 1; the durable document shapes they write are
kept deliberately close to the target design.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request, Response
from google.api_core import exceptions as gapi_exceptions
from google.auth.exceptions import RefreshError
from google.auth.transport import requests as google_requests
from google.cloud import firestore, secretmanager, tasks_v2
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_google_api

from commitmentos.bootstrap.settings import Settings

HISTORY_PAGE_SIZE = 25


@lru_cache(maxsize=1)
def _firestore_client(project: str) -> firestore.Client:
    return firestore.Client(project=project)


@lru_cache(maxsize=1)
def _tasks_client() -> tasks_v2.CloudTasksClient:
    return tasks_v2.CloudTasksClient()


@lru_cache(maxsize=1)
def _secrets_client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def _access_secret(ref: str) -> str:
    return _secrets_client().access_secret_version(name=ref).payload.data.decode("utf-8")


@lru_cache(maxsize=1)
def _controlled_credentials(oauth_client_ref: str, refresh_token_ref: str) -> Credentials:
    client_config = json.loads(_access_secret(oauth_client_ref))["web"]
    return Credentials(
        token=None,
        refresh_token=_access_secret(refresh_token_ref),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
    )


def _verify_delivery(request: Request, expected_audience: str, expected_email: str) -> dict:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = google_id_token.verify_oauth2_token(
            header[7:], google_requests.Request(), audience=expected_audience
        )
    except Exception as error:
        raise HTTPException(status_code=403, detail="invalid delivery token") from error
    if claims.get("email") != expected_email or not claims.get("email_verified"):
        raise HTTPException(status_code=403, detail="unexpected delivery identity")
    return claims


def build_spike_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    project = settings.google_cloud_project

    def _sync_request_ref() -> firestore.DocumentReference:
        return (
            _firestore_client(project)
            .collection("sync_requests")
            .document(f"gmail:{settings.controlled_user_id}")
        )

    def _cursor_ref() -> firestore.DocumentReference:
        return (
            _firestore_client(project)
            .collection("sync_cursors")
            .document(f"gmail:{settings.controlled_user_id}")
        )

    @router.post("/internal/pubsub/gmail", status_code=204)
    async def receive_gmail(request: Request) -> Response:
        _verify_delivery(
            request, settings.pubsub_oidc_audience, settings.pubsub_service_account
        )
        envelope = await request.json()
        message = envelope.get("message") or {}
        try:
            payload = json.loads(base64.b64decode(message.get("data", "")).decode("utf-8"))
            email_address = payload["emailAddress"]
            history_id = int(payload["historyId"])
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="malformed gmail notification") from error
        if email_address != settings.controlled_email:
            raise HTTPException(status_code=403, detail="unexpected mailbox")

        now = datetime.now(timezone.utc)
        client = _firestore_client(project)
        request_ref = _sync_request_ref()

        transaction = client.transaction()

        @firestore.transactional
        def _coalesce(txn: firestore.Transaction) -> int:
            snapshot = request_ref.get(transaction=txn)
            existing = snapshot.to_dict() if snapshot.exists else {}
            latest = max(int(existing.get("latest_history_id", 0)), history_id)
            txn.set(
                request_ref,
                {
                    "source": "gmail",
                    "user_id": settings.controlled_user_id,
                    "latest_history_id": latest,
                    "status": "pending",
                    "delivery_count": firestore.Increment(1),
                    "updated_at": now,
                    "created_at": existing.get("created_at", now),
                },
                merge=True,
            )
            return latest

        latest_history_id = _coalesce(transaction)

        queue_path = _tasks_client().queue_path(
            project, settings.google_cloud_region, settings.source_sync_queue
        )
        task_id = f"gmailsync-{settings.controlled_user_id}-{latest_history_id}"
        task_body = json.dumps(
            {
                "schema": settings.task_schema_version,
                "source": "gmail",
                "user_id": settings.controlled_user_id,
                "latest_history_id": latest_history_id,
            }
        ).encode("utf-8")
        try:
            _tasks_client().create_task(
                parent=queue_path,
                task={
                    "name": f"{queue_path}/tasks/{task_id}",
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
            pass  # duplicate Pub/Sub delivery converged on the same named task
        return Response(status_code=204)

    @router.post("/internal/tasks/source-sync")
    async def source_sync(request: Request) -> dict:
        _verify_delivery(
            request, settings.tasks_oidc_audience, settings.tasks_service_account
        )
        body = await request.json()
        if body.get("user_id") != settings.controlled_user_id:
            raise HTTPException(status_code=400, detail="unexpected task payload")
        if body.get("source") != "gmail":
            raise HTTPException(status_code=400, detail="unexpected task payload")

        cursor_snapshot = _cursor_ref().get()
        if not cursor_snapshot.exists:
            raise HTTPException(
                status_code=409, detail="no published gmail cursor; register the watch first"
            )
        published_history_id = int(cursor_snapshot.get("published_history_id"))

        credentials = _controlled_credentials(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )
        gmail = build_google_api("gmail", "v1", credentials=credentials, cache_discovery=False)
        try:
            response = (
                gmail.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=published_history_id,
                    maxResults=HISTORY_PAGE_SIZE,
                )
                .execute()
            )
        except RefreshError:
            # Invalid credential: persist the visible state, stop dependent
            # work, serve no cached data. Cache cleared so a reauthorized
            # secret version is picked up on the next attempt.
            _controlled_credentials.cache_clear()
            _sync_request_ref().set(
                {
                    "status": "reauth_required",
                    "auth_error": "invalid_grant",
                    "auth_failed_at": datetime.now(timezone.utc),
                },
                merge=True,
            )
            return {"status": "reauth_required", "detail": "credential refresh failed; dependent work stopped"}

        history_records = response.get("history", [])
        candidate_history_id = int(response.get("historyId", published_history_id))
        next_page_token_present = "nextPageToken" in response

        _sync_request_ref().set(
            {
                "status": "fetched",
                "fetched_at": datetime.now(timezone.utc),
                "published_history_id_unchanged": published_history_id,
                "candidate_history_id": candidate_history_id,
                "history_records_seen": len(history_records),
                "next_page_token_present": next_page_token_present,
            },
            merge=True,
        )
        return {
            "status": "fetched",
            "history_records": len(history_records),
            "published_history_id_unchanged": published_history_id,
            "candidate_history_id": candidate_history_id,
            "next_page_token_present": next_page_token_present,
        }

    return router
