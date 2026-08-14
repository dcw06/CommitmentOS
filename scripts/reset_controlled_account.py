"""Audited controlled-account cleanup — the documented developer command (D4).

Preview-then-execute with a typed confirmation phrase. The command targets
only recorded app-owned work-block events (the writer independently refuses
anything without valid CommitmentOS ownership properties), purges the
domain documents for the controlled user, retains the activity and
reconciliation-run audit history plus all source-truth machinery, and
records the cleanup itself in the audit timeline. 5B uses this command as
the between-golden-runs reset.

Usage:
    .venv/bin/python scripts/reset_controlled_account.py preview
    .venv/bin/python scripts/reset_controlled_account.py run \
        --confirm "cleanup <user_id> events=N documents=M"

The exact confirmation phrase is printed by `preview`. `run` re-derives the
preview and aborts if durable state changed since the phrase was issued.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.application.commands.cleanup_controlled_account import (  # noqa: E402
    CleanupControlledAccount,
)
from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.infrastructure.firestore.cleanup import (  # noqa: E402
    FirestoreCleanupDocumentStore,
)


def _build(settings: Settings) -> tuple[CleanupControlledAccount, str]:
    container = ApplicationContainer.build(settings)
    command = CleanupControlledAccount(
        container.unit_of_work(),
        container.calendar_writer(),
        FirestoreCleanupDocumentStore(container.firestore_client()),
        container.clock,
    )
    return command, settings.controlled_user_id


async def _preview(settings: Settings) -> int:
    command, user_id = _build(settings)
    preview = await command.preview(user_id)
    print(json.dumps(
        {
            "user_id": preview.user_id,
            "document_counts": dict(preview.document_counts),
            "owned_event_targets": [
                {
                    "calendar_event_id": target.calendar_event_id,
                    "work_block_id": target.work_block_id,
                    "commitment_id": target.commitment_id,
                    "has_snapshot_etag": target.expected_observed_event_etag is not None,
                }
                for target in preview.owned_event_targets
            ],
        },
        indent=2,
    ))
    print()
    print("To execute, run:")
    print(
        "  .venv/bin/python scripts/reset_controlled_account.py run "
        f'--confirm "{command.confirmation_phrase(preview)}"'
    )
    return 0


async def _run(settings: Settings, confirmation: str) -> int:
    command, user_id = _build(settings)
    preview = await command.preview(user_id)
    result = await command.execute(
        user_id, preview, confirmation, trace_id="trace-developer-cleanup"
    )
    print(json.dumps(
        {
            "executed": result.executed,
            "abort_reason": result.abort_reason,
            "events_canceled": result.events_canceled,
            "events_already_absent": result.events_already_absent,
            "events_skipped_stale_or_unsynchronized": (
                result.events_skipped_stale_or_unsynchronized
            ),
            "documents_purged": dict(result.documents_purged),
        },
        indent=2,
    ))
    return 0 if result.executed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preview")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args()

    settings = Settings.load()
    if arguments.command == "preview":
        return asyncio.run(_preview(settings))
    return asyncio.run(_run(settings, arguments.confirm))


if __name__ == "__main__":
    raise SystemExit(main())
