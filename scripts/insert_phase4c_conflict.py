"""Insert the single unrelated Phase 4C gate conflict meeting.

Companion to `run_phase4c_gate.py`. The gate runbook expects one unrelated
meeting over the armed owned interval; this helper performs that insertion
through the Calendar API with the controlled credential when the owner
delegates the step. The event body is a plain timed meeting with no
CommitmentOS extended properties, exactly overlapping the armed interval
printed by `arm`.

Usage:
    .venv/bin/python scripts/insert_phase4c_conflict.py \
        --start 2026-08-14T16:00:00Z --end 2026-08-14T17:00:00Z
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
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
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
        .insert(
            calendarId=settings.calendar_id,
            body={
                "summary": args.summary,
                "start": {"dateTime": args.start},
                "end": {"dateTime": args.end},
            },
        )
        .execute()
    )
    print("inserted", event["id"], "created", event.get("created"))


if __name__ == "__main__":
    main()
