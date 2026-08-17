"""Phase 5A — manual completion and the §4.5 terminal invariant.

Golden audit step 17: explicit completion stores evidence, closes the
commitment, and closes pending check-in requests through the continuation
observation — while never fabricating verified minutes and never reopening
on later reconciliation, replay, or periodic safety.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from conftest import CONTROLLED_USER, Phase1App
from test_phase4c_always_on_safety import block, commitment, create_live_plan

from commitmentos.application.commands.change_commitment import (
    ChangeCommitmentLifecycleRequest,
    ReopenCommitmentRequest,
    SetCommitmentPriorityRequest,
)
from commitmentos.application.commands.complete_commitment import (
    CompleteCommitmentRequest,
)
from commitmentos.application.commands.run_maintenance import MaintenanceKind
from commitmentos.application.dto import CommandStatus
from commitmentos.application.queries.get_commitment import GetCommitment
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.application.queries.get_today import GetToday
from commitmentos.application.queries.list_commitments import ListCommitments
from commitmentos.contracts.observations import ObservationType
from commitmentos.domain.audit.models import ActivityEventType
from commitmentos.domain.commitments.models import LifecycleStatus
from commitmentos.domain.progress.models import WorkBlockExecutionState
from commitmentos.infrastructure.firestore.repositories.implementations import (
    FirestoreCalendarSnapshotRepository,
)


def _completion_request(
    app: Phase1App,
    commitment_id: str,
    *,
    key: str = "complete-001",
    revision: int | None = None,
    note: str | None = "Sent the revised proposal ahead of the review.",
) -> CompleteCommitmentRequest:
    stored_revision = (
        revision
        if revision is not None
        else app.store["commitments"][commitment_id]["revision"]
    )
    return CompleteCommitmentRequest(
        commitment_id=commitment_id,
        idempotency_key=key,
        completed_at=app.clock.now(),
        expected_revision=stored_revision,
        note=note,
    )


async def _live_commitment_id(app: Phase1App) -> str:
    return next(iter(app.store["commitments"]))


class TestGoldenDryRun:
    async def test_golden_dry_run_reaches_completed_through_real_workflow(
        self, app: Phase1App
    ) -> None:
        """5A exit: seed → effort → plan → Calendar events → verified
        check-in → elapse → explicit completion, all through production
        commands, ending terminal with honest verified minutes."""
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)

        blocks = sorted(
            app.store["work_blocks"].items(),
            key=lambda item: item[1]["scheduled_start"],
        )
        first_block_id, first_block = blocks[0]
        from commitmentos.application.commands.record_work_check_in import (
            WorkCheckInRequest,
        )

        app.clock.current = first_block["scheduled_end"] + timedelta(minutes=5)
        # The safety scan elapses the block into the durable check-in
        # request state before the user can record verified minutes.
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-dry-run-first-elapse"
        )
        await app.drain()
        elapsed_block = app.store["work_blocks"][first_block_id]
        assert elapsed_block["execution_state"] == (
            WorkBlockExecutionState.AWAITING_CHECK_IN.value
        )
        check_in = await app.record_work_check_in.execute(
            app.actor(),
            WorkCheckInRequest(
                work_block_id=first_block_id,
                idempotency_key="dry-run-check-in-1",
                completed=True,
                verified_minutes=60,
                checked_in_at=app.clock.now(),
                expected_revision=elapsed_block["revision"],
            ),
            "trace-dry-run-check-in",
        )
        assert check_in.status == CommandStatus.COMPLETED
        await app.drain()

        # The remaining blocks elapse without check-ins.
        last_end = max(row["scheduled_end"] for _, row in blocks)
        app.clock.current = last_end + timedelta(minutes=5)
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-dry-run-elapse"
        )
        await app.drain()

        result = await app.complete_commitment.execute(
            app.actor(),
            _completion_request(app, commitment_id, note="Dry-run completion."),
            "trace-dry-run-complete",
        )
        assert result.status == CommandStatus.COMPLETED
        await app.drain()

        stored = app.store["commitments"][commitment_id]
        assert stored["lifecycle_status"] == LifecycleStatus.COMPLETED.value
        assert stored["completion_evidence_id"] is not None
        evidence = app.store["evidence"][stored["completion_evidence_id"]]
        # 60 verified minutes against a 180-minute estimate: closure is
        # honest history, never padded (§4.5).
        assert evidence["verified_minutes_at_completion"] == 60
        assert evidence["confirmed_minutes_at_completion"] == 180
        total_verified = sum(
            row["verified_minutes"] for row in app.store["work_blocks"].values()
        )
        assert total_verified == 60
        assert all(
            row["execution_state"]
            in (
                WorkBlockExecutionState.COMPLETED.value,
                WorkBlockExecutionState.MISSED.value,
                WorkBlockExecutionState.CANCELED.value,
            )
            for row in app.store["work_blocks"].values()
        )


class TestCompleteCommitmentCommand:
    async def test_completion_writes_evidence_terminal_state_and_observation(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)

        result = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )

        assert result.status == CommandStatus.COMPLETED
        stored = app.store["commitments"][commitment_id]
        assert stored["lifecycle_status"] == LifecycleStatus.COMPLETED.value
        assert stored["completion_evidence_id"] == result.identifiers["evidence_id"]
        assert stored["completed_at"] is not None

        evidence = app.store["evidence"][result.identifiers["evidence_id"]]
        assert evidence["evidence_type"] == "commitment_completion"
        assert evidence["actor"] == CONTROLLED_USER
        # §4.5: closure below the confirmed estimate is honest history.
        assert evidence["verified_minutes_at_completion"] == 0
        assert evidence["confirmed_minutes_at_completion"] == 180

        observation = app.store["source_observations"][
            result.identifiers["observation_id"]
        ]
        assert observation["observation_type"] == (
            ObservationType.COMPLETION_CONFIRMED.value
        )
        assert any(
            row["event_type"] == ActivityEventType.COMPLETION_RECORDED.value
            and row["payload"]["commitment_id"] == commitment_id
            for row in app.store["activity_events"].values()
        )

    async def test_completion_replay_is_idempotent(self, app: Phase1App) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        request = _completion_request(app, commitment_id)
        first = await app.complete_commitment.execute(
            app.actor(), request, "trace-complete-1"
        )
        assert first.status == CommandStatus.COMPLETED
        snapshot = copy.deepcopy(
            {
                "commitments": app.store["commitments"],
                "evidence": app.store["evidence"],
                "source_observations": app.store["source_observations"],
            }
        )

        replay = await app.complete_commitment.execute(
            app.actor(), request, "trace-complete-2"
        )
        assert replay.status == CommandStatus.NO_OP
        assert replay.error_code == "completion_already_recorded"
        assert app.store["commitments"] == snapshot["commitments"]
        assert app.store["evidence"] == snapshot["evidence"]
        assert app.store["source_observations"] == snapshot["source_observations"]

    async def test_second_completion_act_mutates_nothing(self, app: Phase1App) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        first = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete-1"
        )
        assert first.status == CommandStatus.COMPLETED
        commitments_before = copy.deepcopy(app.store["commitments"])

        second = await app.complete_commitment.execute(
            app.actor(),
            _completion_request(
                app,
                commitment_id,
                key="complete-002",
                revision=app.store["commitments"][commitment_id]["revision"],
            ),
            "trace-complete-3",
        )
        assert second.status == CommandStatus.NO_OP
        assert second.error_code == "commitment_already_completed"
        assert app.store["commitments"] == commitments_before

    async def test_idempotency_key_reuse_for_different_facts_is_rejected(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete-1"
        )
        conflicting = await app.complete_commitment.execute(
            app.actor(),
            _completion_request(
                app,
                commitment_id,
                note="A different act reusing the same key",
                revision=app.store["commitments"][commitment_id]["revision"],
            ),
            "trace-complete-4",
        )
        assert conflicting.status == CommandStatus.TERMINAL_FAILURE
        assert conflicting.error_code == "idempotency_key_reused"

    async def test_revision_conflict_and_missing_target(self, app: Phase1App) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        stale = await app.complete_commitment.execute(
            app.actor(),
            _completion_request(
                app,
                commitment_id,
                revision=app.store["commitments"][commitment_id]["revision"] + 5,
            ),
            "trace-complete-stale",
        )
        assert stale.status == CommandStatus.NO_OP
        assert stale.error_code == "commitment_revision_conflict"
        assert app.store["commitments"][commitment_id]["lifecycle_status"] != (
            LifecycleStatus.COMPLETED.value
        )

        missing = await app.complete_commitment.execute(
            app.actor(),
            CompleteCommitmentRequest(
                commitment_id="commitment-does-not-exist",
                idempotency_key="complete-404",
                completed_at=app.clock.now(),
                expected_revision=1,
            ),
            "trace-complete-404",
        )
        assert missing.status == CommandStatus.TERMINAL_FAILURE
        assert missing.error_code == "commitment_not_found"


class TestCompletionContinuation:
    async def test_pending_check_in_requests_close_without_verified_minutes(
        self, app: Phase1App
    ) -> None:
        item = commitment(app, "closing")
        elapsed = block(app, item.commitment_id)

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.commitments.save(item, None)
            await repositories.work_blocks.save(elapsed, None)

        await app.uow.run(_save)
        # Elapse the block into the durable check-in request state.
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-activate"
        )
        await app.run_reconciliation_tasks()
        app.clock.advance(31 * 60)
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-elapse"
        )
        await app.run_reconciliation_tasks()
        stored_block = app.store["work_blocks"][elapsed.work_block_id]
        assert stored_block["execution_state"] == (
            WorkBlockExecutionState.AWAITING_CHECK_IN.value
        )

        result = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, item.commitment_id), "trace-complete"
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()

        stored_block = app.store["work_blocks"][elapsed.work_block_id]
        assert stored_block["execution_state"] == WorkBlockExecutionState.MISSED.value
        assert stored_block["verified_minutes"] == 0
        stored = app.store["commitments"][item.commitment_id]
        assert stored["lifecycle_status"] == LifecycleStatus.COMPLETED.value
        assert any(
            row["event_type"] == ActivityEventType.COMPLETION_RECORDED.value
            and row["payload"].get("check_in_requests_closed")
            == [elapsed.work_block_id]
            for row in app.store["activity_events"].values()
        )

    async def test_future_planned_blocks_release_calendar_time(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        planned_ids = [
            block_id
            for block_id, row in app.store["work_blocks"].items()
            if row["execution_state"] == WorkBlockExecutionState.PLANNED.value
        ]
        assert planned_ids
        outbox_before = set(app.store.get("action_outbox", {}))

        result = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()

        for block_id in planned_ids:
            assert app.store["work_blocks"][block_id]["execution_state"] == (
                WorkBlockExecutionState.CANCELED.value
            )
        cancel_intents = [
            row
            for outbox_id, row in app.store["action_outbox"].items()
            if outbox_id not in outbox_before
        ]
        assert len(cancel_intents) == len(planned_ids)
        for row in cancel_intents:
            assert row["mutation"]["action_type"] == "cancel"
            # If-Match comes from the published snapshot store, never a
            # provider read (plan §9.2).
            snapshot_id = FirestoreCalendarSnapshotRepository.snapshot_id(
                row["mutation"]["calendar_id"],
                row["mutation"]["calendar_event_id"],
            )
            snapshot = app.store["calendar_event_snapshots"][snapshot_id]
            assert row["mutation"]["expected_observed_event_etag"] == (
                snapshot["observed_event_etag"]
            )

        await app.run_calendar_action_tasks()
        for row in cancel_intents:
            key = (
                row["mutation"]["calendar_id"],
                row["mutation"]["calendar_event_id"],
            )
            assert key not in app.calendar.live_events()
            assert ("cancel", row["mutation"]["calendar_event_id"]) in (
                app.calendar.mutation_log
            )

    async def test_replayed_continuation_converges_and_stays_closed(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        result = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        await app.run_calendar_action_tasks()
        await app.run_reconciliation_tasks()

        commitments_before = copy.deepcopy(app.store["commitments"])
        blocks_before = copy.deepcopy(app.store["work_blocks"])
        outbox_before = copy.deepcopy(app.store["action_outbox"])

        # Redeliver the completion continuation like a Cloud Tasks retry.
        app._reconciliation_cursor = 0  # noqa: SLF001 - test redelivery control
        await app.run_reconciliation_tasks()
        await app.run_calendar_action_tasks()

        assert app.store["commitments"] == commitments_before
        assert app.store["work_blocks"] == blocks_before
        assert app.store["action_outbox"] == outbox_before
        assert app.store["commitments"][commitment_id]["lifecycle_status"] == (
            LifecycleStatus.COMPLETED.value
        )

    async def test_later_safety_and_replan_keep_completion_terminal(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )
        await app.drain()

        verified_before = {
            block_id: row["verified_minutes"]
            for block_id, row in app.store["work_blocks"].items()
        }
        app.clock.advance(24 * 60 * 60)
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-post-completion-safety"
        )
        await app.drain()

        stored = app.store["commitments"][commitment_id]
        assert stored["lifecycle_status"] == LifecycleStatus.COMPLETED.value
        assert stored["completion_evidence_id"] is not None
        for block_id, row in app.store["work_blocks"].items():
            assert row["verified_minutes"] == verified_before[block_id]

    async def test_completed_commitment_leaves_the_portfolio_demand_set(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        runs_before = set(app.store["planner_runs"])
        await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )
        await app.drain()

        replans = [
            row
            for run_id, row in app.store["planner_runs"].items()
            if run_id not in runs_before and row["status"] == "published"
        ]
        assert replans
        for run in replans:
            assert commitment_id not in run["commitment_order"]
            assert all(
                allocation["commitment_id"] != commitment_id
                for allocation in run["allocations"]
            )

        status = await GetSystemStatus(app.uow, app.clock).execute(CONTROLLED_USER)
        assert all(
            failure["state"] != "no_feasible_plan" for failure in status.failure_states
        )


class TestCommitmentCorrections:
    async def test_pause_releases_blocks_and_resume_replans_current_demand(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        planned_ids = {
            block_id
            for block_id, row in app.store["work_blocks"].items()
            if row["execution_state"] == WorkBlockExecutionState.PLANNED.value
        }
        outbox_before = set(app.store["action_outbox"])
        app.clock.advance(60)

        result = await app.change_commitment.change_lifecycle(
            app.actor(),
            ChangeCommitmentLifecycleRequest(
                commitment_id,
                app.store["commitments"][commitment_id]["revision"],
                LifecycleStatus.PAUSED,
            ),
            "trace-pause",
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()

        assert app.store["commitments"][commitment_id]["lifecycle_status"] == (
            LifecycleStatus.PAUSED.value
        )
        assert planned_ids
        assert all(
            app.store["work_blocks"][block_id]["execution_state"]
            == WorkBlockExecutionState.CANCELED.value
            for block_id in planned_ids
        )
        new_outbox = [
            row
            for outbox_id, row in app.store["action_outbox"].items()
            if outbox_id not in outbox_before
        ]
        assert len(new_outbox) == len(planned_ids)
        assert all(row["mutation"]["action_type"] == "cancel" for row in new_outbox)
        assert any(
            row["status"] == "published"
            and commitment_id not in row["commitment_order"]
            for row in app.store["planner_runs"].values()
        )

        app.clock.advance(60)
        resumed = await app.change_commitment.change_lifecycle(
            app.actor(),
            ChangeCommitmentLifecycleRequest(
                commitment_id,
                app.store["commitments"][commitment_id]["revision"],
                LifecycleStatus.ACTIVE,
            ),
            "trace-resume",
        )
        assert resumed.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        assert app.store["commitments"][commitment_id]["lifecycle_status"] == (
            LifecycleStatus.ACTIVE.value
        )
        assert any(
            row["execution_state"] == WorkBlockExecutionState.PLANNED.value
            and block_id not in planned_ids
            for block_id, row in app.store["work_blocks"].items()
        )

    async def test_dashboard_queries_expose_current_portfolio_and_outcomes(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)

        page = await ListCommitments(app.uow).execute(
            CONTROLLED_USER, None, None, None, 25
        )
        summary = next(
            item for item in page.items if item["commitment_id"] == commitment_id
        )
        assert summary["projection"]["current"] is True
        assert summary["portfolio"]["allocated_work_minutes"] == 180
        assert summary["portfolio"]["shortfall_minutes"] == 0
        assert "shared_buffer_minutes" in summary["portfolio"]

        detail = await GetCommitment(app.uow).execute(
            CONTROLLED_USER, commitment_id
        )
        assert detail is not None
        assert detail.portfolio == summary["portfolio"]

        status = GetSystemStatus(app.uow, app.clock)
        today = await GetToday(app.uow, app.clock, status).execute(
            CONTROLLED_USER, "America/Los_Angeles"
        )
        assert today.outcome_strip["commitments_kept_feasible"] == 1
        assert today.outcome_strip["work_minutes_reserved"] == 180
        assert today.outcome_strip["manual_reschedules_avoided"] == 0

    async def test_priority_and_reopen_are_revision_guarded_and_replan(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        commitment_id = await _live_commitment_id(app)
        assert app.store["commitments"][commitment_id]["last_reconciled_at"] is not None
        revision = app.store["commitments"][commitment_id]["revision"]

        priority = await app.change_commitment.set_priority(
            app.actor(),
            SetCommitmentPriorityRequest(commitment_id, revision, 25),
            "trace-priority",
        )
        assert priority.status == CommandStatus.COMPLETED
        assert app.store["commitments"][commitment_id]["explicit_priority"] == 25
        await app.run_reconciliation_tasks()

        completed = await app.complete_commitment.execute(
            app.actor(), _completion_request(app, commitment_id), "trace-complete"
        )
        assert completed.status == CommandStatus.COMPLETED
        await app.drain()
        completed_revision = app.store["commitments"][commitment_id]["revision"]

        reopened = await app.change_commitment.reopen(
            app.actor(),
            ReopenCommitmentRequest(commitment_id, completed_revision),
            "trace-reopen",
        )
        assert reopened.status == CommandStatus.COMPLETED
        stored = app.store["commitments"][commitment_id]
        assert stored["lifecycle_status"] == LifecycleStatus.ACTIVE.value
        assert stored["completion_evidence_id"] is None
        assert stored["completed_at"] is None
        assert any(
            row["event_type"] == ActivityEventType.COMMITMENT_REOPENED.value
            for row in app.store["activity_events"].values()
        )
        assert app.task_dispatcher.reconciliation_tasks[-1][1].observation_id == (
            reopened.identifiers["observation_id"]
        )
