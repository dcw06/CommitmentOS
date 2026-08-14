"""Phase 1 gate proofs: seeded vertical slice, controls, recovery.

Covers plan §17 Phase 1 and the checklist Part II D1 rows:
- seeded observation -> commitment -> effort approval -> plan approval ->
  outbox -> authenticated executor -> action_result -> follow-up reconciliation
- observation commits before dispatch; reconciliation queue only
- automatic-action pause holds queued work; resume revalidates
- approval survives a process restart
- write-before-enqueue crash gap repaired by maintenance (blocker B1)
- Calendar 412 -> stale_precondition with no action_result and one sync request
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from conftest import (
    CALENDAR_ID,
    CONTROLLED_USER,
    TASK_SCHEMA_VERSION,
    Phase1App,
    restarted,
)

from commitmentos.application.dto import CommandStatus, ControlChangeRequest
from commitmentos.contracts.observations import ObservationType, ReconciliationStatus
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType
from commitmentos.domain.actions.models import (
    CalendarActionType,
    DispatchStatus,
    ExecutionStatus,
)
from commitmentos.workflows.reconciliation.phase1_workflow import derive_calendar_event_id


async def _list_pending_approvals(app: Phase1App):
    async def _load(repositories):
        return list(await repositories.approvals.list_pending(CONTROLLED_USER))

    return await app.uow.read(_load)


async def _approve(app: Phase1App, approval, **extra):
    decision = {"decision": "approve", **extra}
    return await app.resolve_approval.execute(
        app.actor(),
        approval["approval_id"],
        decision,
        approval["revision"],
        trace_id="trace-test",
    )


async def _advance_to_plan_dispatch(app: Phase1App):
    """Seeded observation through both approvals until outbox tasks exist."""
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()

    approvals = await _list_pending_approvals(app)
    assert len(approvals) == 1 and approvals[0]["request_type"] == "effort_confirmation"
    result = await _approve(app, approvals[0], confirmed_minutes=180)
    assert result.status == CommandStatus.COMPLETED
    await app.run_reconciliation_tasks()

    approvals = await _list_pending_approvals(app)
    assert len(approvals) == 1 and approvals[0]["request_type"] == "initial_plan_approval"
    assert len(approvals[0]["payload"]["proposed_blocks"]) == 3
    result = await _approve(app, approvals[0])
    assert result.status == CommandStatus.COMPLETED
    await app.run_reconciliation_tasks()


class TestSeededVerticalSlice:
    async def test_full_slice_reaches_calendar_and_verifies(self, app: Phase1App) -> None:
        await _advance_to_plan_dispatch(app)

        # Three outbox actions were written transactionally and dispatched.
        assert len(app.task_dispatcher.calendar_action_tasks) == 3
        outbox_rows = app.store["action_outbox"]
        assert len(outbox_rows) == 3
        assert all(row["dispatch_status"] == "queued" for row in outbox_rows.values())

        # The executor performs the mutations and records terminal results.
        results = await app.run_calendar_action_tasks()
        assert all(result.status == CommandStatus.COMPLETED for result in results)
        assert len(app.calendar.events) == 3
        assert all(row["execution_status"] == "succeeded" for row in outbox_rows.values())

        # Stable identity: every Calendar event ID derives from its block.
        for row in outbox_rows.values():
            expected = derive_calendar_event_id(CALENDAR_ID, row["work_block_id"])
            assert row["mutation"]["calendar_event_id"] == expected
            props = app.calendar.events[(CALENDAR_ID, expected)]["private_properties"]
            assert props["managed_by"] == "commitmentos"
            assert props["work_block_id"] == row["work_block_id"]

        # One action_result observation per action, delivered through the
        # reconciliation queue (never Pub/Sub), then processed.
        action_results = [
            data
            for data in app.store["source_observations"].values()
            if data["observation_type"] == ObservationType.ACTION_RESULT.value
        ]
        assert len(action_results) == 3
        await app.run_reconciliation_tasks()
        assert all(
            data["reconciliation_status"] == ReconciliationStatus.PROCESSED.value
            for data in app.store["source_observations"].values()
        )

    async def test_replayed_executor_task_creates_no_duplicate(self, app: Phase1App) -> None:
        await _advance_to_plan_dispatch(app)
        await app.run_calendar_action_tasks()
        mutations_before = list(app.calendar.mutation_log)

        # Redeliver every calendar task (Cloud Tasks is at-least-once).
        app._calendar_action_cursor = 0
        results = await app.run_calendar_action_tasks()
        assert all(result.status == CommandStatus.NO_OP for result in results)
        assert app.calendar.mutation_log == mutations_before
        assert len(app.calendar.events) == 3

    async def test_replayed_seeded_observation_creates_one_commitment(
        self, app: Phase1App
    ) -> None:
        first = await app.seed_golden_observation()
        second = await app.seed_golden_observation()
        assert first == second  # deterministic observation identity
        await app.run_reconciliation_tasks()
        assert len(app.store["commitments"]) == 1
        assert len(await _list_pending_approvals(app)) == 1


class TestExecutionControls:
    async def test_pause_holds_queued_action_and_resume_revalidates(
        self, app: Phase1App
    ) -> None:
        await _advance_to_plan_dispatch(app)
        assert len(app.task_dispatcher.calendar_action_tasks) == 3

        # Pause automatic actions before any executor runs.
        result = await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="automatic_actions",
                target_mode="paused",
                reason="test pause",
                expected_control_epoch=1,
            ),
            trace_id="trace-pause",
        )
        assert result.status == CommandStatus.COMPLETED

        # Deliver the queued action tasks repeatedly: everything holds, no mutation.
        for _ in range(2):
            app._calendar_action_cursor = 0
            results = await app.run_calendar_action_tasks()
            assert all(r.status == CommandStatus.HELD for r in results)
        assert len(app.calendar.events) == 0
        assert app.calendar.mutation_log == []
        held = [
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.HELD_BY_CONTROL.value
        ]
        assert len(held) == 3

        # The pause observation itself reconciles without releasing anything.
        await app.run_reconciliation_tasks()
        assert len(app.calendar.events) == 0

        # Resume: revalidation supersedes held intent and reissues it with the
        # new control epoch; only then do mutations execute.
        result = await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="automatic_actions",
                target_mode="enabled",
                reason="test resume",
                expected_control_epoch=2,
            ),
            trace_id="trace-resume",
        )
        assert result.status == CommandStatus.COMPLETED
        await app.drain()

        statuses = sorted(
            row["execution_status"] for row in app.store["action_outbox"].values()
        )
        assert statuses.count(ExecutionStatus.SUPERSEDED.value) == 3
        assert statuses.count(ExecutionStatus.SUCCEEDED.value) == 3
        assert len(app.calendar.events) == 3
        replacement_epochs = {
            row["expected_control_epoch"]
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.SUCCEEDED.value
        }
        assert replacement_epochs == {3}

    async def test_pause_before_plan_approval_holds_new_intent(self, app: Phase1App) -> None:
        """The deployed pause-proof ordering: outbox intent written while
        automatic actions are already paused is held at dispatch time and
        never creates a Calendar task until resume revalidation."""
        await app.seed_golden_observation()
        await app.run_reconciliation_tasks()
        approval = (await _list_pending_approvals(app))[0]
        await _approve(app, approval, confirmed_minutes=180)
        await app.run_reconciliation_tasks()
        plan_approval = (await _list_pending_approvals(app))[0]

        await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="automatic_actions",
                target_mode="paused",
                reason="pause before plan approval",
                expected_control_epoch=1,
            ),
            trace_id="trace-pause",
        )
        await app.run_reconciliation_tasks()

        await _approve(app, plan_approval)
        await app.run_reconciliation_tasks()

        # Intent exists and is durably held; no Calendar task was created.
        held = [
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.HELD_BY_CONTROL.value
        ]
        assert len(held) == 3
        assert app.task_dispatcher.calendar_action_tasks == []
        assert len(app.calendar.events) == 0

        await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="automatic_actions",
                target_mode="enabled",
                reason="resume",
                expected_control_epoch=2,
            ),
            trace_id="trace-resume",
        )
        await app.drain()
        assert len(app.calendar.events) == 3
        succeeded = [
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.SUCCEEDED.value
        ]
        assert len(succeeded) == 3

    async def test_monitoring_pause_holds_observation_and_resume_redispatches(
        self, app: Phase1App
    ) -> None:
        # Pause monitoring first.
        await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="monitoring",
                target_mode="paused",
                reason="test pause",
                expected_control_epoch=1,
            ),
            trace_id="trace-pause",
        )
        observation_id = await app.seed_golden_observation()
        row = app.store["source_observations"][observation_id]
        assert row["reconciliation_status"] == ReconciliationStatus.HELD_BY_CONTROL.value
        # No reconciliation task was created for the held observation.
        seeded_tasks = [
            task
            for _, task in app.task_dispatcher.reconciliation_tasks
            if task.observation_id == observation_id
        ]
        assert seeded_tasks == []
        assert app.store.get("commitments", {}) == {}

        # Resume monitoring: held observation redispatches with a new
        # dispatch generation and processes from current facts.
        await app.change_control.execute(
            app.actor(),
            ControlChangeRequest(
                control_name="monitoring",
                target_mode="enabled",
                reason="test resume",
                expected_control_epoch=2,
            ),
            trace_id="trace-resume",
        )
        row = app.store["source_observations"][observation_id]
        assert row["reconciliation_status"] == ReconciliationStatus.QUEUED.value
        assert row["dispatch_generation"] == 1
        await app.run_reconciliation_tasks()
        assert len(app.store["commitments"]) == 1

    async def test_stale_dispatch_generation_task_is_acknowledged_without_work(
        self, app: Phase1App
    ) -> None:
        observation_id = await app.seed_golden_observation()
        # Simulate a pre-pause task name arriving after a resume bumped the
        # generation: rewrite the durable generation ahead of the task's.
        row = app.store["source_observations"][observation_id]
        row["dispatch_generation"] = 5
        results = await app.run_reconciliation_tasks()
        assert len(results) == 1
        assert results[0].status == CommandStatus.NO_OP
        assert results[0].error_code == "stale_dispatch_generation"
        assert app.store.get("commitments", {}) == {}


class TestDurability:
    async def test_approval_survives_process_restart(self, app: Phase1App) -> None:
        await app.seed_golden_observation()
        await app.run_reconciliation_tasks()
        approvals = await _list_pending_approvals(app)
        assert len(approvals) == 1

        # Cloud Run recycle: new process, same Firestore state and queue.
        fresh = restarted(app)
        approvals = await _list_pending_approvals(fresh)
        assert len(approvals) == 1
        result = await _approve(fresh, approvals[0], confirmed_minutes=180)
        assert result.status == CommandStatus.COMPLETED
        await fresh.run_reconciliation_tasks()
        approvals = await _list_pending_approvals(fresh)
        assert approvals and approvals[0]["request_type"] == "initial_plan_approval"

    async def test_double_resolution_lets_one_decision_win(self, app: Phase1App) -> None:
        await app.seed_golden_observation()
        await app.run_reconciliation_tasks()
        approval = (await _list_pending_approvals(app))[0]
        first = await _approve(app, approval, confirmed_minutes=180)
        second = await _approve(app, approval, confirmed_minutes=90)
        assert first.status == CommandStatus.COMPLETED
        assert second.status == CommandStatus.NO_OP
        assert second.error_code == "approval_already_resolved"
        commitment = next(iter(app.store["commitments"].values()))
        assert commitment["effort"]["confirmed_minutes"] == 180
        # Exactly one approval_resolved continuation observation exists.
        continuations = [
            data
            for data in app.store["source_observations"].values()
            if data["observation_type"] == ObservationType.APPROVAL_RESOLVED.value
        ]
        assert len(continuations) == 1

    async def test_crash_gap_repaired_by_maintenance(self, app: Phase1App) -> None:
        """Blocker B1: record committed, task creation failed, dispatcher repairs."""
        await app.seed_golden_observation()
        await app.run_reconciliation_tasks()
        approval = (await _list_pending_approvals(app))[0]

        # The continuation observation commits, then Cloud Tasks fails.
        app.task_dispatcher.fail_next_enqueues = 1
        result = await _approve(app, approval, confirmed_minutes=180)
        assert result.status == CommandStatus.COMPLETED  # durable work exists
        observation_id = result.identifiers["observation_id"]
        row = app.store["source_observations"][observation_id]
        assert row["reconciliation_status"] == ReconciliationStatus.QUEUED.value
        names_before = [name for name, _ in app.task_dispatcher.reconciliation_tasks]
        assert all(observation_id not in str(task.observation_id) for _, task in
                   app.task_dispatcher.reconciliation_tasks)

        # Maintenance finds the queued-but-undispatched record and recreates
        # the same named task.
        outcome = await app.maintenance.dispatch_pending("trace-maintenance")
        assert outcome.status == CommandStatus.COMPLETED
        names_after = [name for name, _ in app.task_dispatcher.reconciliation_tasks]
        assert len(names_after) == len(names_before) + 1
        await app.run_reconciliation_tasks()
        approvals = await _list_pending_approvals(app)
        assert approvals and approvals[0]["request_type"] == "initial_plan_approval"


class TestCalendarPreconditions:
    async def test_412_marks_stale_precondition_without_action_result(
        self, app: Phase1App
    ) -> None:
        await _advance_to_plan_dispatch(app)
        await app.run_calendar_action_tasks()
        await app.run_reconciliation_tasks()
        await app.synchronize_source.execute(
            SourceSyncTaskV1(
                schema_version=TASK_SCHEMA_VERSION,
                sync_request_id=f"calendar:{CONTROLLED_USER}:precondition-fixture",
                sync_generation_id="precondition-fixture",
                page_sequence=0,
                source=SourceType.CALENDAR,
                user_id=CONTROLLED_USER,
                trace_id="trace-precondition-fixture",
            )
        )
        observations_before = len(app.store["source_observations"])

        # Craft a patch intent whose expected etag is stale: another editor
        # touches the event after the etag was recorded.
        outbox_id, row = next(iter(app.store["action_outbox"].items()))
        event_key = (CALENDAR_ID, row["mutation"]["calendar_event_id"])
        recorded_etag = app.calendar.events[event_key]["etag"]
        app.calendar.events[event_key]["etag"] = app.calendar.next_etag()

        async def _load(repositories):
            return await repositories.outbox.get(outbox_id)

        succeeded = await app.uow.read(_load)
        patch_action = replace(
            succeeded,
            outbox_id="patch-" + outbox_id[:8],
            action_idempotency_key=succeeded.action_idempotency_key.replace(
                ":insert:", ":patch:"
            ),
            mutation=replace(
                succeeded.mutation,
                action_type=CalendarActionType.PATCH,
                desired_start=succeeded.mutation.desired_start + timedelta(hours=2),
                desired_end=succeeded.mutation.desired_end + timedelta(hours=2),
                expected_observed_event_etag=recorded_etag,
            ),
            dispatch_status=DispatchStatus.PENDING,
            execution_status=ExecutionStatus.PENDING,
            attempts=0,
            mutation_response=None,
        )

        async def _create(repositories):
            await repositories.outbox.create(patch_action)

        await app.uow.run(_create)
        await app.outbox_dispatcher.dispatch(patch_action.outbox_id)
        results = await app.run_calendar_action_tasks()
        assert len(results) == 1
        assert results[0].error_code == "calendar_precondition_stale"

        stored = app.store["action_outbox"][patch_action.outbox_id]
        assert stored["execution_status"] == ExecutionStatus.STALE_PRECONDITION.value
        # No overwrite: the event keeps the intervening editor's etag and time.
        assert app.calendar.events[event_key]["etag"] != recorded_etag
        assert ("patch", row["mutation"]["calendar_event_id"]) not in app.calendar.mutation_log
        # No action_result observation was emitted for the 412.
        assert len(app.store["source_observations"]) == observations_before
        # One durable Calendar synchronization request exists.
        sync_request = app.store["sync_requests"][f"calendar:{CONTROLLED_USER}"]
        assert sync_request["reason"] == "stale_precondition"
        assert sync_request["status"] == "pending"

    async def test_retryable_failure_records_attempt_without_action_result(
        self, app: Phase1App
    ) -> None:
        await _advance_to_plan_dispatch(app)
        app.calendar_writer.retryable_failures_remaining = 1
        results = await app.run_calendar_action_tasks()
        retryable = [r for r in results if r.status == CommandStatus.RETRYABLE_FAILURE]
        assert len(retryable) == 1
        failed_rows = [
            row
            for row in app.store["action_outbox"].values()
            if row["execution_status"] == ExecutionStatus.RETRYABLE_FAILED.value
        ]
        assert len(failed_rows) == 1
        assert failed_rows[0]["attempts"] == 1
        action_results = [
            data
            for data in app.store["source_observations"].values()
            if data["observation_type"] == ObservationType.ACTION_RESULT.value
        ]
        assert len(action_results) == 2  # only the two successful inserts
