"""Phase 0 Section 5 — Calendar watch registration, poke, renewal, and inspection.

Usage (from the repo root; --service-url is the deployed Cloud Run URL):

    python scripts/calendar_watch_spike.py register --service-url https://...
    python scripts/calendar_watch_spike.py status
    python scripts/calendar_watch_spike.py poke      # insert a spike-owned test event
    python scripts/calendar_watch_spike.py renew --service-url https://...
    python scripts/calendar_watch_spike.py stop
    python scripts/calendar_watch_spike.py cleanup   # delete spike-owned events only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from google.cloud import firestore, secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_google_api

from commitmentos.bootstrap.settings import Settings

SPIKE_PROPERTY = {"managed_by": "commitmentos-spike"}
BASELINE_PAGE_LIMIT = 20


def _secret(settings_ref: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    return sm.access_secret_version(name=settings_ref).payload.data.decode("utf-8")


def _controlled_credentials(settings: Settings) -> Credentials:
    client_config = json.loads(_secret(settings.oauth_client_secret_ref))["web"]
    return Credentials(
        token=None,
        refresh_token=_secret(settings.controlled_refresh_token_secret_ref),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
    )


def _calendar_service(settings: Settings):
    return build_google_api(
        "calendar", "v3", credentials=_controlled_credentials(settings), cache_discovery=False
    )


def _channel_doc(settings: Settings) -> firestore.DocumentReference:
    client = firestore.Client(project=settings.google_cloud_project)
    return client.collection("calendar_channels").document(settings.controlled_user_id)


def _cursor_doc(settings: Settings) -> firestore.DocumentReference:
    client = firestore.Client(project=settings.google_cloud_project)
    return client.collection("sync_cursors").document(
        f"calendar:{settings.controlled_user_id}"
    )


def _baseline_sync_token(settings: Settings, service) -> str:
    page_token = None
    for _ in range(BASELINE_PAGE_LIMIT):
        response = (
            service.events()
            .list(calendarId=settings.calendar_id, maxResults=50, pageToken=page_token)
            .execute()
        )
        if "nextSyncToken" in response:
            return response["nextSyncToken"]
        page_token = response.get("nextPageToken")
        if page_token is None:
            break
    raise RuntimeError("baseline pagination did not produce a sync token within bounds")


def _open_channel(settings: Settings, service, service_url: str) -> dict:
    channel_id = str(uuid.uuid4())
    secret = _secret(settings.calendar_channel_secret_ref).strip()
    response = (
        service.events()
        .watch(
            calendarId=settings.calendar_id,
            body={
                "id": channel_id,
                "type": "web_hook",
                "address": f"{service_url.rstrip('/')}{settings.calendar_webhook_path}",
                "token": secret,
            },
        )
        .execute()
    )
    expiration = datetime.fromtimestamp(int(response["expiration"]) / 1000, tz=timezone.utc)
    return {
        "channel_id": channel_id,
        "resource_id": response["resourceId"],
        "resource_uri": response.get("resourceUri", ""),
        "expiration": expiration,
        "token_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
    }


def cmd_register(settings: Settings, service_url: str) -> int:
    service = _calendar_service(settings)
    sync_token = _baseline_sync_token(settings, service)
    _cursor_doc(settings).set(
        {
            "source": "calendar",
            "user_id": settings.controlled_user_id,
            "published_sync_token": sync_token,
            "baseline_recorded_at": datetime.now(timezone.utc),
        }
    )
    channel = _open_channel(settings, service, service_url)
    _channel_doc(settings).set(
        {
            **channel,
            "calendar_id": settings.calendar_id,
            "registered_at": datetime.now(timezone.utc),
        }
    )
    print("=== calendar watch registered ===")
    print(f"channel_id: {channel['channel_id']}")
    print(f"resource_id: {channel['resource_id']}")
    print(f"expiration_utc: {channel['expiration'].isoformat(timespec='seconds')}")
    print(f"token_hash_prefix: {channel['token_hash'][:12]}…")
    print("published sync token recorded; raw channel secret persisted nowhere")
    return 0


def cmd_status(settings: Settings) -> int:
    client = firestore.Client(project=settings.google_cloud_project)
    docs = {
        "calendar_channels": client.collection("calendar_channels").document(
            settings.controlled_user_id
        ),
        "sync_cursors": _cursor_doc(settings),
        "sync_requests": client.collection("sync_requests").document(
            f"calendar:{settings.controlled_user_id}"
        ),
    }
    for label, ref in docs.items():
        snapshot = ref.get()
        print(f"--- {label} ---")
        if not snapshot.exists:
            print("(absent)")
            continue
        for key, value in sorted(snapshot.to_dict().items()):
            if key == "published_sync_token":
                value = f"<present, {len(str(value))} chars>"
            print(f"{key}: {value}")
    return 0


def cmd_poke(settings: Settings) -> int:
    service = _calendar_service(settings)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    event = (
        service.events()
        .insert(
            calendarId=settings.calendar_id,
            body={
                "summary": "CommitmentOS spike poke",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(minutes=15)).isoformat()},
                "extendedProperties": {"private": SPIKE_PROPERTY},
            },
        )
        .execute()
    )
    print(f"poke event inserted: {event['id']}")
    return 0


def cmd_renew(settings: Settings, service_url: str) -> int:
    snapshot = _channel_doc(settings).get()
    if not snapshot.exists:
        print("no channel registered")
        return 1
    old = snapshot.to_dict()
    service = _calendar_service(settings)
    channel = _open_channel(settings, service, service_url)
    _channel_doc(settings).set(
        {
            **channel,
            "calendar_id": settings.calendar_id,
            "registered_at": datetime.now(timezone.utc),
            "previous_channel_id": old["channel_id"],
            "previous_resource_id": old["resource_id"],
            "renewed_at": datetime.now(timezone.utc),
        }
    )
    service.channels().stop(
        body={"id": old["channel_id"], "resourceId": old["resource_id"]}
    ).execute()
    print("=== calendar watch renewed ===")
    print(f"new_channel_id: {channel['channel_id']}")
    print(f"new_expiration_utc: {channel['expiration'].isoformat(timespec='seconds')}")
    print(f"old_channel_stopped: {old['channel_id']}")
    return 0


def cmd_stop(settings: Settings) -> int:
    snapshot = _channel_doc(settings).get()
    if not snapshot.exists:
        print("no channel registered")
        return 1
    channel = snapshot.to_dict()
    _calendar_service(settings).channels().stop(
        body={"id": channel["channel_id"], "resourceId": channel["resource_id"]}
    ).execute()
    print(f"channel stopped: {channel['channel_id']}")
    return 0


def cmd_cleanup(settings: Settings) -> int:
    service = _calendar_service(settings)
    response = (
        service.events()
        .list(
            calendarId=settings.calendar_id,
            privateExtendedProperty="managed_by=commitmentos-spike",
            maxResults=50,
        )
        .execute()
    )
    events = response.get("items", [])
    for event in events:
        service.events().delete(calendarId=settings.calendar_id, eventId=event["id"]).execute()
        print(f"deleted spike event: {event['id']}")
    if not events:
        print("no spike-owned events found")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "renew"):
        p = sub.add_parser(name)
        p.add_argument("--service-url", required=True)
    for name in ("status", "poke", "stop", "cleanup"):
        sub.add_parser(name)
    args = parser.parse_args()

    settings = Settings.load()
    if args.command == "register":
        return cmd_register(settings, args.service_url)
    if args.command == "renew":
        return cmd_renew(settings, args.service_url)
    if args.command == "poke":
        return cmd_poke(settings)
    if args.command == "stop":
        return cmd_stop(settings)
    if args.command == "cleanup":
        return cmd_cleanup(settings)
    return cmd_status(settings)


if __name__ == "__main__":
    raise SystemExit(main())
