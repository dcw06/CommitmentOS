"""Phase 0 Section 4 — Gmail watch registration and delivery-state inspection.

Usage (from the repo root):

    python scripts/gmail_watch_spike.py register   # users.watch -> topic, records cursor
    python scripts/gmail_watch_spike.py status     # show cursor + sync-request state
    python scripts/gmail_watch_spike.py stop       # users.stop (teardown)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from google.cloud import firestore, secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_google_api

from commitmentos.bootstrap.settings import Settings


def _controlled_credentials(settings: Settings) -> Credentials:
    sm = secretmanager.SecretManagerServiceClient()

    def access(ref: str) -> str:
        return sm.access_secret_version(name=ref).payload.data.decode("utf-8")

    client_config = json.loads(access(settings.oauth_client_secret_ref))["web"]
    return Credentials(
        token=None,
        refresh_token=access(settings.controlled_refresh_token_secret_ref),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
    )


def cmd_register(settings: Settings) -> int:
    gmail = build_google_api(
        "gmail", "v1", credentials=_controlled_credentials(settings), cache_discovery=False
    )
    response = (
        gmail.users()
        .watch(userId="me", body={"topicName": settings.gmail_pubsub_topic})
        .execute()
    )
    history_id = int(response["historyId"])
    expiration_ms = int(response["expiration"])
    expiration = datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc)

    client = firestore.Client(project=settings.google_cloud_project)
    client.collection("sync_cursors").document(f"gmail:{settings.controlled_user_id}").set(
        {
            "source": "gmail",
            "user_id": settings.controlled_user_id,
            "published_history_id": history_id,
            "watch_expiration": expiration,
            "watch_registered_at": datetime.now(timezone.utc),
        }
    )
    print("=== gmail watch registered ===")
    print(f"initial_history_id: {history_id}")
    print(f"watch_expiration_utc: {expiration.isoformat(timespec='seconds')}")
    print("published cursor recorded in sync_cursors/gmail:" + settings.controlled_user_id)
    return 0


def cmd_status(settings: Settings) -> int:
    client = firestore.Client(project=settings.google_cloud_project)
    for collection in ("sync_cursors", "sync_requests"):
        snapshot = (
            client.collection(collection)
            .document(f"gmail:{settings.controlled_user_id}")
            .get()
        )
        print(f"--- {collection} ---")
        if not snapshot.exists:
            print("(absent)")
            continue
        for key, value in sorted(snapshot.to_dict().items()):
            print(f"{key}: {value}")
    return 0


def cmd_stop(settings: Settings) -> int:
    gmail = build_google_api(
        "gmail", "v1", credentials=_controlled_credentials(settings), cache_discovery=False
    )
    gmail.users().stop(userId="me").execute()
    print("gmail watch stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("register")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()

    settings = Settings.load()
    if args.command == "register":
        return cmd_register(settings)
    if args.command == "status":
        return cmd_status(settings)
    return cmd_stop(settings)


if __name__ == "__main__":
    raise SystemExit(main())
