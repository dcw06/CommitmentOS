"""Remove a Phase 4C gate conflict meeting inserted by its companion script.

Deletes one event by ID only after verifying it is NOT app-owned (no
CommitmentOS extended properties) and its summary matches the expected
conflict-meeting title, so an owned or unrelated real event can never be
deleted by accident.

Usage:
    .venv/bin/python scripts/remove_phase4c_conflict.py --event-id <id> \
        [--summary "Department budget review"]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.infrastructure.google.credentials import (  # noqa: E402
    ControlledCredentialsProvider,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--summary", default="Department budget review")
    args = parser.parse_args()

    from googleapiclient.discovery import build

    settings = Settings.load()
    provider = ControlledCredentialsProvider(
        settings.oauth_client_secret_ref,
        settings.controlled_refresh_token_secret_ref,
    )
    service = build(
        "calendar", "v3", credentials=provider.credentials(), cache_discovery=False
    )
    event = (
        service.events()
        .get(calendarId=settings.calendar_id, eventId=args.event_id)
        .execute()
    )
    private = (event.get("extendedProperties") or {}).get("private") or {}
    if private.get("managed_by"):
        raise SystemExit(f"refusing: event {args.event_id} is app-owned")
    if event.get("summary") != args.summary:
        raise SystemExit(
            f"refusing: summary {event.get('summary')!r} != expected {args.summary!r}"
        )
    service.events().delete(
        calendarId=settings.calendar_id, eventId=args.event_id
    ).execute()
    print("deleted", args.event_id)


if __name__ == "__main__":
    main()
