from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from conftest import CALENDAR_ID, CONTROLLED_USER, TASK_SCHEMA_VERSION, Phase1App

from commitmentos.application.dto import CommandStatus
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType
from commitmentos.domain.actions.models import (
    CalendarActionType,
    DispatchStatus,
    ExecutionStatus,
)
from commitmentos.domain.audit.models import ActivityEventType
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
)
from commitmentos.domain.progress.models import UserEditState


async def pending_approval(app: Phase1App, request_type: str):
    async def _load(repositories):
        return [
            approval
            for approval in await repositories.approvals.list_pending(CONTROLLED_USER)
            if approval["request_type"] == request_type
        ]

    values = await app.uow.read(_load)
    return values[0] if values else None


async def sync_calendar(app: Phase1App, suffix: str) -> None:
    result = await app.synchronize_source.execute(
        SourceSyncTaskV1(
            schema_version=TASK_SCHEMA_VERSION,
            sync_request_id=f"calendar:{CONTROLLED_USER}:{suffix}",
            sync_generation_id=f"calendar-{suffix}",
            page_sequence=0,
            source=SourceType.CALENDAR,
            user_id=CONTROLLED_USER,
            trace_id=f"trace-calendar-{suffix}",
        )
    )
    assert result.status == CommandStatus.COMPLETED


async def create_live_plan(app: Phase1App) -> None:
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()
    effort = await pending_approval(app, "effort_confirmation")
    assert effort is not None
    await app.resolve_approval.execute(
        app.actor(),
        effort["approval_id"],
        {"decision": "approve", "confirmed_minutes": 180},
        effort["revision"],
        "trace-4b-effort",
    )
    await app.run_reconciliation_tasks()
    plan = await pending_approval(app, "initial_plan_approval")
    assert plan is not None
    await app.resolve_approval.execute(
        app.actor(),
        plan["approval_id"],
        {"decision": "approve"},
        plan["revision"],
        "trace-4b-plan",
    )
    await app.run_reconciliation_tasks()
    action_results = await app.run_calendar_action_tasks()
    assert action_results and all(
        result.status == CommandStatus.COMPLETED for result in action_results
    )
    await app.run_reconciliation_tasks()
    await sync_calendar(app, "baseline")


def planned_blocks(app: Phase1App):
    return sorted(
        (
            dict(block) | {"work_block_id": work_block_id}
            for work_block_id, block in app.store["work_blocks"].items()
        ),
        key=lambda block: (block["scheduled_start"], block["work_block_id"]),
    )


class TestStableAutomaticRepair:
    async def test_environmental_conflict_moves_one_block_and_preserves_the_rest(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        before = {block["work_block_id"]: dict(block) for block in planned_blocks(app)}
        target = planned_blocks(app)[1]
        app.calendar.events[(CALENDAR_ID, "external-conflict")] = {
            "status": "confirmed",
            "start": target["scheduled_start"],
            "end": target["scheduled_end"],
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "environmental-conflict")
        results = await app.run_reconciliation_tasks()
        assert any(result.status == CommandStatus.COMPLETED for result in results), [
            (result.status, result.error_code) for result in results
        ]

        after = {block["work_block_id"]: block for block in planned_blocks(app)}
        changed = [
            work_block_id
            for work_block_id in before
            if (
                before[work_block_id]["scheduled_start"],
                before[work_block_id]["scheduled_end"],
            )
            != (
                after[work_block_id]["scheduled_start"],
                after[work_block_id]["scheduled_end"],
            )
        ]
        assert changed == [target["work_block_id"]]
        assert len(app.store["action_outbox"]) == outbox_count + 1
        repair_action = next(
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.PENDING.value
        )
        assert repair_action["mutation"]["action_type"] == CalendarActionType.PATCH.value
        snapshot = next(
            row
            for row in app.store["calendar_event_snapshots"].values()
            if row["observed_work_block_id"] == target["work_block_id"]
        )
        assert (
            repair_action["mutation"]["expected_observed_event_etag"]
            == snapshot["observed_event_etag"]
        )
        activities = app.store["activity_events"].values()
        risk_events = [
            row
            for row in activities
            if row["event_type"] == ActivityEventType.RISK_CHANGED.value
            and "risk_before_repair" in row["payload"]
        ]
        assert risk_events
        policy_event = next(
            row
            for row in activities
            if row["event_type"] == ActivityEventType.POLICY_DECIDED.value
            and row["payload"].get("threshold_version") == "policy_thresholds_v1"
        )
        assert policy_event["payload"]["disposition"] == "automatic"
        assert policy_event["payload"]["undo_available"] is True

    async def test_unrelated_overdue_commitment_does_not_block_automatic_repair(
        self,
        app: Phase1App,
    ) -> None:
        """Live Phase 4 gate finding (2026-08-14): a pre-existing overdue
        commitment makes every portfolio plan infeasible, which must not
        convert an unrelated in-policy one-block repair into an approval."""
        await create_live_plan(app)
        now = app.clock.now()
        overdue = Commitment(
            commitment_id="overdue-unrelated",
            user_id=CONTROLLED_USER,
            revision=1,
            source_thread_id="thread-overdue-unrelated",
            semantic_fingerprint="fingerprint-overdue-unrelated",
            title="Overdue unrelated commitment",
            description="",
            ownership_type=OwnershipType.MY_COMMITMENT,
            owner={"type": "user"},
            beneficiary={"display_name": "Reviewer"},
            deadline=Deadline(
                value=now - timedelta(hours=2),
                timezone="UTC",
                confidence=1.0,
                evidence_id="evidence-overdue-unrelated",
                source_expression="fixture deadline",
                rule_version="test",
            ),
            effort=Effort(120, 1.0, 120, now),
            lifecycle_status=LifecycleStatus.ACTIVE,
            completion_evidence_id=None,
            completed_at=None,
            plan_revision=1,
            projection=None,
            policy_profile="default_personal",
            created_at=now - timedelta(days=1),
            updated_at=now,
        )

        async def _save(repositories):  # noqa: ANN001
            await repositories.commitments.save(overdue, None)

        await app.uow.run(_save)

        before = {block["work_block_id"]: dict(block) for block in planned_blocks(app)}
        target = planned_blocks(app)[1]
        app.calendar.events[(CALENDAR_ID, "external-conflict-overdue")] = {
            "status": "confirmed",
            "start": target["scheduled_start"],
            "end": target["scheduled_end"],
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "environmental-conflict-overdue")
        results = await app.run_reconciliation_tasks()
        assert any(result.status == CommandStatus.COMPLETED for result in results), [
            (result.status, result.error_code) for result in results
        ]

        assert await pending_approval(app, "action_approval") is None
        after = {block["work_block_id"]: block for block in planned_blocks(app)}
        changed = [
            work_block_id
            for work_block_id in before
            if (
                before[work_block_id]["scheduled_start"],
                before[work_block_id]["scheduled_end"],
            )
            != (
                after[work_block_id]["scheduled_start"],
                after[work_block_id]["scheduled_end"],
            )
        ]
        assert changed == [target["work_block_id"]]
        assert len(app.store["action_outbox"]) == outbox_count + 1
        policy_event = next(
            row
            for row in app.store["activity_events"].values()
            if row["event_type"] == ActivityEventType.POLICY_DECIDED.value
            and row["payload"].get("threshold_version") == "policy_thresholds_v1"
        )
        assert policy_event["payload"]["disposition"] == "automatic"

        repair_run = next(
            row
            for row in app.store["planner_runs"].values()
            if "_repair" in (row.get("risk_audit") or {})
            and row["status"] == "published"
        )
        assert repair_run["feasible"] is False
        overdue_allocation = next(
            allocation
            for allocation in repair_run["allocations"]
            if allocation["commitment_id"] == "overdue-unrelated"
        )
        assert overdue_allocation["shortfall_minutes"] == 120
        assert overdue_allocation["risk_level"] == "overdue"

    async def test_valid_user_move_is_adopted_without_calendar_mutation(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        block = planned_blocks(app)[0]
        event_key = (CALENDAR_ID, block["calendar_event_id"])
        event = app.calendar.events[event_key]
        event["start"] += timedelta(days=1)
        event["end"] += timedelta(days=1)
        event["etag"] = app.calendar.next_etag()
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "valid-user-move")
        await app.run_reconciliation_tasks()

        stored = app.store["work_blocks"][block["work_block_id"]]
        assert stored["scheduled_start"] == event["start"]
        assert stored["scheduled_end"] == event["end"]
        assert stored["user_edit_state"] == UserEditState.ADOPTED.value
        assert len(app.store["action_outbox"]) == outbox_count
        assert any(
            row["event_type"] == ActivityEventType.USER_MOVE_ADOPTED.value
            for row in app.store["activity_events"].values()
        )

    async def test_cancel_uses_snapshot_etag_as_immutable_if_match_source(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        block = planned_blocks(app)[0]

        async def _load(repositories):
            actions = await repositories.outbox.list_for_work_block(
                CONTROLLED_USER,
                block["work_block_id"],
                10,
            )
            snapshot = await repositories.calendar_snapshots.get(
                CALENDAR_ID,
                block["calendar_event_id"],
            )
            return actions[0], snapshot

        succeeded, snapshot = await app.uow.read(_load)
        assert snapshot is not None and snapshot.observed_event_etag is not None
        cancel = replace(
            succeeded,
            outbox_id=f"cancel-{succeeded.outbox_id[:12]}",
            action_idempotency_key=f"cancel:{succeeded.action_idempotency_key}",
            mutation=replace(
                succeeded.mutation,
                action_type=CalendarActionType.CANCEL,
                desired_start=None,
                desired_end=None,
                expected_observed_event_etag=snapshot.observed_event_etag,
            ),
            dispatch_status=DispatchStatus.PENDING,
            execution_status=ExecutionStatus.PENDING,
            attempts=0,
            mutation_response=None,
        )

        async def _create(repositories):
            await repositories.outbox.create(cancel)

        await app.uow.run(_create)
        await app.outbox_dispatcher.dispatch(cancel.outbox_id)
        results = await app.run_calendar_action_tasks()
        assert results[0].status == CommandStatus.COMPLETED
        assert (
            cancel.mutation.expected_observed_event_etag
            == snapshot.observed_event_etag
        )
        assert (
            "cancel",
            block["calendar_event_id"],
        ) in app.calendar.mutation_log

    async def test_forced_412_resynchronizes_snapshot_etag_and_resumes_intent(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        target = planned_blocks(app)[1]
        event_key = (CALENDAR_ID, target["calendar_event_id"])
        app.calendar.events[(CALENDAR_ID, "external-412-conflict")] = {
            "status": "confirmed",
            "start": target["scheduled_start"],
            "end": target["scheduled_end"],
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        await sync_calendar(app, "412-conflict")
        await app.run_reconciliation_tasks()
        first_repair = next(
            (outbox_id, row)
            for outbox_id, row in app.store["action_outbox"].items()
            if row["execution_status"] == ExecutionStatus.PENDING.value
        )
        first_outbox_id, first_row = first_repair
        desired_start = first_row["mutation"]["desired_start"]
        old_snapshot_etag = first_row["mutation"]["expected_observed_event_etag"]

        # Provider truth changes after planner publication but before I/O.
        app.calendar.events[event_key]["etag"] = app.calendar.next_etag()
        results = await app.run_calendar_action_tasks()
        stale_result = next(
            result
            for result in results
            if result.identifiers.get("outbox_id") == first_outbox_id
        )
        assert stale_result.error_code == "calendar_precondition_stale"
        assert (
            app.store["action_outbox"][first_outbox_id]["execution_status"]
            == ExecutionStatus.STALE_PRECONDITION.value
        )
        assert any(
            task.sync_request_id == f"calendar:{CONTROLLED_USER}"
            for _, task in app.task_dispatcher.source_sync_tasks
        )

        sync_results = await app.run_source_sync_tasks()
        assert sync_results and sync_results[-1].status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        resumed = [
            (outbox_id, row)
            for outbox_id, row in app.store["action_outbox"].items()
            if row["execution_status"] == ExecutionStatus.PENDING.value
            and outbox_id != first_outbox_id
        ]
        assert len(resumed) == 1
        resumed_id, resumed_row = resumed[0]
        assert resumed_row["mutation"]["desired_start"] == desired_start
        assert (
            resumed_row["mutation"]["expected_observed_event_etag"]
            == app.calendar.events[event_key]["etag"]
        )
        assert resumed_row["mutation"]["expected_observed_event_etag"] != old_snapshot_etag

        resumed_results = await app.run_calendar_action_tasks()
        assert any(
            result.identifiers.get("outbox_id") == resumed_id
            and result.status == CommandStatus.COMPLETED
            and result.error_code is None
            for result in resumed_results
        )
        assert app.calendar.events[event_key]["start"] == desired_start

    async def test_three_block_repair_becomes_action_approval_without_outbox(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        for index, block in enumerate(planned_blocks(app)):
            app.calendar.events[(CALENDAR_ID, f"extensive-conflict-{index}")] = {
                "status": "confirmed",
                "start": block["scheduled_start"],
                "end": block["scheduled_end"],
                "etag": app.calendar.next_etag(),
                "private_properties": {},
            }
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "extensive-repair")
        await app.run_reconciliation_tasks()

        approval = await pending_approval(app, "action_approval")
        assert approval is not None
        assert approval["payload"]["moved_block_count"] == 3
        assert approval["payload"]["reason_codes"] == [
            "more_than_two_blocks_changed"
        ]
        assert len(approval["payload"]["mutations"]) == 3
        assert sum(
            row["request_type"] == "action_approval"
            for row in app.store["approvals"].values()
        ) == 1
        assert len(app.store["action_outbox"]) == outbox_count


class TestCalendarEditDecisions:
    async def test_restore_choice_writes_one_conditional_patch_to_the_approved_slot(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        block = planned_blocks(app)[0]
        approved_start = block["scheduled_start"]
        approved_end = block["scheduled_end"]
        event = app.calendar.events[(CALENDAR_ID, block["calendar_event_id"])]
        event["start"] = app.clock.now() - timedelta(hours=2)
        event["end"] = app.clock.now() - timedelta(hours=1)
        event["etag"] = app.calendar.next_etag()
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "invalid-user-move-restore")
        await app.run_reconciliation_tasks()
        approval = await pending_approval(app, "calendar_invalid_move_decision")
        assert approval is not None

        resolved = await app.resolve_approval.execute(
            app.actor(),
            approval["approval_id"],
            {"decision": "approve", "choice": "restore_approved_slot"},
            approval["revision"],
            "trace-restore-approved-slot",
        )
        assert resolved.status == CommandStatus.COMPLETED
        results = await app.run_reconciliation_tasks()
        assert results[-1].status == CommandStatus.COMPLETED

        assert len(app.store["action_outbox"]) == outbox_count + 1
        repair = next(
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.PENDING.value
        )
        assert repair["mutation"]["action_type"] == CalendarActionType.PATCH.value
        assert repair["mutation"]["desired_start"] == approved_start
        assert repair["mutation"]["desired_end"] == approved_end
        snapshot = next(
            row
            for row in app.store["calendar_event_snapshots"].values()
            if row["observed_work_block_id"] == block["work_block_id"]
        )
        assert (
            repair["mutation"]["expected_observed_event_etag"]
            == snapshot["observed_event_etag"]
        )
        restored_block = app.store["work_blocks"][block["work_block_id"]]
        assert restored_block["user_edit_state"] == UserEditState.NONE.value

    async def test_invalid_move_creates_one_choice_and_no_calendar_action(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        block = planned_blocks(app)[0]
        event = app.calendar.events[(CALENDAR_ID, block["calendar_event_id"])]
        event["start"] = app.clock.now() - timedelta(hours=2)
        event["end"] = app.clock.now() - timedelta(hours=1)
        event["etag"] = app.calendar.next_etag()
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "invalid-user-move")
        await app.run_reconciliation_tasks()

        approval = await pending_approval(app, "calendar_invalid_move_decision")
        assert approval is not None
        assert approval["payload"]["options"] == [
            "restore_approved_slot",
            "reschedule_safely",
            "pause_commitment",
        ]
        assert (
            app.store["work_blocks"][block["work_block_id"]]["user_edit_state"]
            == UserEditState.INVALID_MOVE.value
        )
        assert len(app.store["action_outbox"]) == outbox_count

    async def test_user_deletion_creates_one_decision_and_never_recreates_silently(
        self,
        app: Phase1App,
    ) -> None:
        await create_live_plan(app)
        block = planned_blocks(app)[0]
        event_key = (CALENDAR_ID, block["calendar_event_id"])
        del app.calendar.events[event_key]
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        cursor["full_resync_required"] = True
        outbox_count = len(app.store["action_outbox"])

        await sync_calendar(app, "user-deletion")
        await app.run_reconciliation_tasks()

        approval = await pending_approval(app, "calendar_user_deleted_decision")
        assert approval is not None, [
            (row["observation_type"], row["reconciliation_status"], row["safe_metadata"])
            for row in app.store["source_observations"].values()
        ]
        assert approval["payload"]["options"] == [
            "reschedule_unfinished",
            "record_completed",
            "pause_commitment",
        ]
        assert (
            app.store["work_blocks"][block["work_block_id"]]["user_edit_state"]
            == UserEditState.USER_DELETED.value
        )
        assert event_key not in app.calendar.events
        assert len(app.store["action_outbox"]) == outbox_count
        deletion_activities = [
            row
            for row in app.store["activity_events"].values()
            if row["event_type"]
            == ActivityEventType.USER_DELETION_REQUIRES_DECISION.value
        ]
        assert len(deletion_activities) == 1

        resolved = await app.resolve_approval.execute(
            app.actor(),
            approval["approval_id"],
            {"decision": "approve", "choice": "reschedule_unfinished"},
            approval["revision"],
            "trace-user-deletion-choice",
        )
        assert resolved.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        explicit_recreation = [
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.PENDING.value
        ]
        assert len(explicit_recreation) == 1
        assert (
            explicit_recreation[0]["mutation"]["action_type"]
            == CalendarActionType.ADOPT.value
        )
