"""D4 audited controlled-account cleanup — preview, guarded execute, audit.

Only recorded app-owned work-block events are targeted (the writer's §9.3
guard independently refuses anything else); unrelated Calendar events are
structurally unreachable; the purge retains audit history and source-truth
machinery; and the cleanup itself lands in the audit timeline.
"""

from __future__ import annotations

import copy
from typing import Any, Sequence

from conftest import CALENDAR_ID, CONTROLLED_USER, Phase1App
from test_phase4c_always_on_safety import create_live_plan

from commitmentos.application.commands.cleanup_controlled_account import (
    CleanupControlledAccount,
)
from commitmentos.domain.audit.models import ActivityEventType


class InMemoryCleanupDocumentStore:
    def __init__(self, store: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._store = store

    def _matching_ids(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
    ) -> list[str]:
        rows = self._store.get(collection, {})
        matched = []
        for document_id, document in rows.items():
            if all(document.get(field) == value for field, op, value in filters):
                matched.append(document_id)
        return sorted(matched)

    async def list_ids_where(self, collection, filters, limit):  # noqa: ANN001
        return self._matching_ids(collection, filters)[:limit]

    async def count_where(self, collection, filters):  # noqa: ANN001
        return len(self._matching_ids(collection, filters))

    async def purge_where(self, collection, filters, limit):  # noqa: ANN001
        targets = self._matching_ids(collection, filters)[:limit]
        for document_id in targets:
            del self._store[collection][document_id]
        return len(targets)


def _cleanup(app: Phase1App) -> CleanupControlledAccount:
    return CleanupControlledAccount(
        app.uow,
        app.calendar_writer,
        InMemoryCleanupDocumentStore(app.store),
        app.clock,
    )


UNRELATED_EVENT_KEY = (CALENDAR_ID, "unrelated-department-meeting")


def _insert_unrelated_event(app: Phase1App) -> None:
    app.calendar.events[UNRELATED_EVENT_KEY] = {
        "etag": app.calendar.next_etag(),
        "status": "confirmed",
        "summary": "Department budget review",
    }


class TestCleanupPreview:
    async def test_preview_targets_only_recorded_app_owned_events(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        _insert_unrelated_event(app)
        preview = await _cleanup(app).preview(CONTROLLED_USER)

        owned_event_ids = {
            row["calendar_event_id"] for row in app.store["work_blocks"].values()
        }
        targeted = {t.calendar_event_id for t in preview.owned_event_targets}
        assert targeted == owned_event_ids
        assert UNRELATED_EVENT_KEY[1] not in targeted
        assert preview.document_counts["commitments"] == 1
        assert preview.document_counts["work_blocks"] == len(owned_event_ids)


class TestCleanupExecute:
    async def test_execute_cancels_owned_purges_documents_and_records_audit(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        _insert_unrelated_event(app)
        unrelated_before = copy.deepcopy(app.calendar.events[UNRELATED_EVENT_KEY])
        sessions_before = copy.deepcopy(app.store.get("web_sessions", {}))
        cursors_before = copy.deepcopy(app.store.get("sync_cursors", {}))

        command = _cleanup(app)
        preview = await command.preview(CONTROLLED_USER)
        owned_keys = [
            (t.calendar_id, t.calendar_event_id) for t in preview.owned_event_targets
        ]
        result = await command.execute(
            CONTROLLED_USER,
            preview,
            command.confirmation_phrase(preview),
            "trace-cleanup",
        )

        assert result.executed
        assert result.events_canceled == len(owned_keys)
        for key in owned_keys:
            assert key not in app.calendar.events
        # Unrelated Calendar events remain byte-identical.
        assert app.calendar.events[UNRELATED_EVENT_KEY] == unrelated_before

        for collection in (
            "commitments",
            "work_blocks",
            "evidence",
            "approvals",
            "source_observations",
            "action_outbox",
            "planner_runs",
        ):
            assert app.store.get(collection, {}) == {}, collection
        # Audit history and source-truth machinery are retained.
        assert app.store["activity_events"]
        assert app.store.get("web_sessions", {}) == sessions_before
        assert app.store.get("sync_cursors", {}) == cursors_before
        cleanup_events = [
            row
            for row in app.store["activity_events"].values()
            if row["event_type"] == ActivityEventType.CONTROLLED_CLEANUP_COMPLETED.value
        ]
        assert len(cleanup_events) == 1
        assert cleanup_events[0]["actor"] == "developer_cleanup"
        assert cleanup_events[0]["payload"]["events_canceled"] == len(owned_keys)

    async def test_wrong_confirmation_aborts_with_zero_changes(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        command = _cleanup(app)
        preview = await command.preview(CONTROLLED_USER)
        store_before = copy.deepcopy(app.store)
        events_before = copy.deepcopy(app.calendar.events)

        result = await command.execute(
            CONTROLLED_USER, preview, "cleanup wrong phrase", "trace-cleanup"
        )
        assert not result.executed
        assert result.abort_reason == "confirmation_mismatch"
        assert app.store == store_before
        assert app.calendar.events == events_before

    async def test_state_drift_since_preview_aborts(self, app: Phase1App) -> None:
        await create_live_plan(app)
        command = _cleanup(app)
        preview = await command.preview(CONTROLLED_USER)
        phrase = command.confirmation_phrase(preview)

        # New durable state lands after the phrase was issued.
        app.store["evidence"]["drift-evidence"] = {
            "evidence_id": "drift-evidence",
            "user_id": CONTROLLED_USER,
            "evidence_type": "work_check_in",
        }
        events_before = copy.deepcopy(app.calendar.events)
        result = await command.execute(CONTROLLED_USER, preview, phrase, "trace-cleanup")
        assert not result.executed
        assert result.abort_reason == "state_changed_since_preview"
        assert app.calendar.events == events_before
        assert "drift-evidence" in app.store["evidence"]

    async def test_stale_approvals_are_disposed(self, app: Phase1App) -> None:
        await create_live_plan(app)
        # Mimic the stale Phase 2 identity approvals and the pre-fix action
        # approval that the live account still carries.
        for approval_id in ("stale-identity-1", "stale-identity-2", "19b1acfb-prefix"):
            app.store["approvals"][approval_id] = {
                "approval_id": approval_id,
                "user_id": CONTROLLED_USER,
                "request_type": "identity_confirmation",
                "status": "pending",
            }
        command = _cleanup(app)
        preview = await command.preview(CONTROLLED_USER)
        result = await command.execute(
            CONTROLLED_USER, preview, command.confirmation_phrase(preview), "trace-cleanup"
        )
        assert result.executed
        assert app.store.get("approvals", {}) == {}

    async def test_target_without_snapshot_is_skipped_not_blindly_deleted(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        command = _cleanup(app)
        preview = await command.preview(CONTROLLED_USER)
        target = preview.owned_event_targets[0]
        # Remove the snapshot so no If-Match truth exists for this event.
        snapshot_ids = [
            snapshot_id
            for snapshot_id, row in app.store["calendar_event_snapshots"].items()
            if row.get("calendar_event_id") == target.calendar_event_id
        ]
        for snapshot_id in snapshot_ids:
            del app.store["calendar_event_snapshots"][snapshot_id]

        fresh_preview = await command.preview(CONTROLLED_USER)
        result = await command.execute(
            CONTROLLED_USER,
            fresh_preview,
            command.confirmation_phrase(fresh_preview),
            "trace-cleanup",
        )
        assert result.executed
        assert result.events_skipped_stale_or_unsynchronized == 1
        # The event survives; provider truth is left to the sync loop.
        assert (target.calendar_id, target.calendar_event_id) in app.calendar.events
