"""§16.4 fault-injection matrix (Phase 5A).

Rows landed here: reconciliation worker-kill with fenced-lease takeover (the
open D1 row — a late original worker cannot commit), executor death while
`action_in_flight` both before and after the Calendar response, the §9.4
create-before-record crash converging through the stable event ID, and
projection corruption blocking stale action execution.

Credited, not duplicated: mid-generation worker death resuming from the
durable checkpoint is proven by the Phase 2 suite
(`test_worker_death_resumes_from_durable_checkpoint`,
`test_stale_fencing_token_cannot_checkpoint_or_publish`) and exercised live
by the Phase 4A fenced barrier/publication proofs.
"""

from __future__ import annotations

import copy

from commitmentos.application.dto import CommandResult, CommandStatus, ReconciliationOutcome
from commitmentos.contracts.observations import ReconciliationStatus
from commitmentos.domain.actions.models import CalendarActionType, CalendarMutation
from commitmentos.domain.progress.models import WorkBlockExecutionState
from tests.fault_injection.harness import harness

CONTROLLED_USER = harness.CONTROLLED_USER


async def _pending_approval(app, request_type: str):  # noqa: ANN001
    async def _load(repositories):  # noqa: ANN001, ANN202
        return [
            value
            for value in await repositories.approvals.list_pending(CONTROLLED_USER)
            if value["request_type"] == request_type
        ]

    return (await app.uow.read(_load))[0]


async def _drive_to_approved_plan(app) -> None:  # noqa: ANN001
    """Golden seed through plan approval: outbox intents committed and
    Calendar-action tasks queued, but no executor delivery yet."""
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()
    effort = await _pending_approval(app, "effort_confirmation")
    await app.resolve_approval.execute(
        app.actor(),
        effort["approval_id"],
        {"decision": "approve", "confirmed_minutes": 180},
        effort["revision"],
        "trace-fault-effort",
    )
    await app.run_reconciliation_tasks()
    plan = await _pending_approval(app, "initial_plan_approval")
    await app.resolve_approval.execute(
        app.actor(),
        plan["approval_id"],
        {"decision": "approve"},
        plan["revision"],
        "trace-fault-plan",
    )
    await app.run_reconciliation_tasks()


def _mutation_from_row(row: dict) -> CalendarMutation:
    mutation = row["mutation"]
    return CalendarMutation(
        action_type=CalendarActionType(mutation["action_type"]),
        calendar_id=mutation["calendar_id"],
        calendar_event_id=mutation["calendar_event_id"],
        work_block_id=mutation["work_block_id"],
        desired_start=mutation["desired_start"],
        desired_end=mutation["desired_end"],
        expected_observed_event_etag=mutation.get("expected_observed_event_etag"),
        private_properties=dict(mutation.get("private_properties", {})),
    )


class TestReconciliationWorkerKillAndTakeover:
    """Open D1 row: one retry takes over after the fenced lease expires,
    and a late original worker cannot commit."""

    async def test_takeover_after_lease_expiry_and_stale_fence_rejection(
        self, app  # noqa: ANN001
    ) -> None:
        await app.seed_golden_observation()
        _, task = app.task_dispatcher.reconciliation_tasks[0]

        # Worker A claims the observation, then dies mid-processing.
        claim_a = await app.reconcile._claim_or_hold(task)  # noqa: SLF001
        assert not isinstance(claim_a, CommandResult)

        # Redelivery inside the lease window cannot take over.
        early = await app.reconcile.execute(task)
        assert early.status == CommandStatus.RETRYABLE_FAILURE
        assert early.error_code == "processing_lease_active"

        # After expiry, worker B's claim bumps the fencing token.
        app.clock.advance(301)
        claim_b = await app.reconcile._claim_or_hold(task)  # noqa: SLF001
        assert not isinstance(claim_b, CommandResult)
        assert (
            claim_b.processing_fence.fencing_token
            != claim_a.processing_fence.fencing_token
        )

        # The late original wakes up and tries to commit a forged outcome
        # with its stale fence: the commit must not land.
        forged = ReconciliationOutcome(
            run_id=claim_a.run_id,
            observation_id=task.observation_id,
            status=ReconciliationStatus.IGNORED.value,
            durable_outcome_ids=(),
            error_code="late_worker_forgery",
            retryable=False,
            processing_fencing_token=claim_a.processing_fence.fencing_token,
        )
        state_before = copy.deepcopy(app.store)
        try:
            await app.reconcile._record_terminal_outcome(  # noqa: SLF001
                claim_a, forged, task.workflow_version
            )
        except Exception:  # noqa: BLE001,S110 - a raise is an acceptable rejection shape
            pass
        assert app.store == state_before

        # Worker B completes the same run over durable state.
        request_b = app.reconcile._workflow_request(task, claim_b)  # noqa: SLF001
        outcome_b = await app.workflow.execute(request_b)
        await app.reconcile._record_terminal_outcome(  # noqa: SLF001
            claim_b, outcome_b, task.workflow_version
        )
        stored = app.store["source_observations"][task.observation_id]
        assert stored["reconciliation_status"] == ReconciliationStatus.PROCESSED.value
        # Exactly one commitment: the takeover did not duplicate the work.
        assert len(app.store["commitments"]) == 1

    async def test_replay_after_takeover_converges_no_op(self, app) -> None:  # noqa: ANN001
        await app.seed_golden_observation()
        _, task = app.task_dispatcher.reconciliation_tasks[0]
        claim_a = await app.reconcile._claim_or_hold(task)  # noqa: SLF001
        assert not isinstance(claim_a, CommandResult)
        app.clock.advance(301)
        completed = await app.reconcile.execute(task)
        assert completed.status == CommandStatus.COMPLETED

        state_before = copy.deepcopy(app.store)
        replay = await app.reconcile.execute(task)
        assert replay.status == CommandStatus.NO_OP
        assert app.store == state_before


class TestExecutorDeathWhileActionInFlight:
    async def test_death_after_calendar_response_converges_on_one_event(
        self, app  # noqa: ANN001
    ) -> None:
        """§9.4 create-before-record: the external insert landed, the record
        did not. Recovery through the stable event ID yields one event."""
        await _drive_to_approved_plan(app)
        outbox_id, row = next(iter(app.store["action_outbox"].items()))
        # Replay the dead worker's external effect and its committed
        # linearization state.
        await app.calendar_writer.insert_or_adopt_owned(_mutation_from_row(row))
        row["execution_status"] = "action_in_flight"
        event_id = row["mutation"]["calendar_event_id"]

        results = await app.run_calendar_action_tasks()
        assert all(result.status == CommandStatus.COMPLETED for result in results)
        # Exactly one event for the crashed action's block, adopted not
        # re-inserted.
        inserts = [
            entry for entry in app.calendar.mutation_log if entry == ("insert", event_id)
        ]
        adopts = [
            entry for entry in app.calendar.mutation_log if entry == ("adopt", event_id)
        ]
        assert len(inserts) == 1
        assert len(adopts) == 1
        assert app.store["action_outbox"][outbox_id]["execution_status"] == "succeeded"

        # The follow-up reconciliation and a full task replay stay convergent.
        await app.drain()
        state_before = copy.deepcopy(app.store)
        app._calendar_action_cursor = 0  # noqa: SLF001 - test redelivery control
        await app.run_calendar_action_tasks()
        assert app.store == state_before

    async def test_death_before_calendar_response_recovers_to_one_event(
        self, app  # noqa: ANN001
    ) -> None:
        """The worker died after linearization but before the provider
        responded: no external event exists. Recovery executes the insert
        exactly once through the stable ID."""
        await _drive_to_approved_plan(app)
        outbox_id, row = next(iter(app.store["action_outbox"].items()))
        row["execution_status"] = "action_in_flight"
        event_id = row["mutation"]["calendar_event_id"]

        results = await app.run_calendar_action_tasks()
        assert all(result.status == CommandStatus.COMPLETED for result in results)
        inserts = [
            entry for entry in app.calendar.mutation_log if entry == ("insert", event_id)
        ]
        assert len(inserts) == 1
        assert app.store["action_outbox"][outbox_id]["execution_status"] == "succeeded"

    async def test_transient_backend_failure_retries_without_duplicates(
        self, app  # noqa: ANN001
    ) -> None:
        await _drive_to_approved_plan(app)
        app.calendar_writer.retryable_failures_remaining = 1
        first_pass = await app.run_calendar_action_tasks()
        assert any(
            result.status == CommandStatus.RETRYABLE_FAILURE for result in first_pass
        )

        # Cloud Tasks redelivers; the retry completes every action once.
        app._calendar_action_cursor = 0  # noqa: SLF001 - test redelivery control
        await app.run_calendar_action_tasks()
        block_event_ids = {
            block["calendar_event_id"] for block in app.store["work_blocks"].values()
        }
        for event_id in block_event_ids:
            inserts = [
                entry
                for entry in app.calendar.mutation_log
                if entry == ("insert", event_id)
            ]
            assert len(inserts) == 1
        assert all(
            value["execution_status"] == "succeeded"
            for value in app.store["action_outbox"].values()
        )


class TestProjectionCorruptionBlocksStaleExecution:
    async def test_corrupted_projection_blocks_action_execution(
        self, app  # noqa: ANN001
    ) -> None:
        """§16.4: a projection whose recorded inputs no longer match
        authoritative facts must block execution, not feed it."""
        await _drive_to_approved_plan(app)
        commitment_id = next(iter(app.store["commitments"]))
        projection = app.store["commitments"][commitment_id]["projection"]
        assert projection is not None
        projection["remaining_minutes"] = projection["remaining_minutes"] + 37

        results = await app.run_calendar_action_tasks()
        assert results
        assert all(result.error_code == "action_stale" for result in results)
        # Zero external mutations and every intent visibly stale.
        assert app.calendar.mutation_log == []
        assert app.calendar.events == {}
        assert all(
            value["execution_status"] == "stale"
            for value in app.store["action_outbox"].values()
        )
        # No block ever left `planned`, and no verified minute appeared.
        for block in app.store["work_blocks"].values():
            assert block["execution_state"] == WorkBlockExecutionState.PLANNED.value
            assert block["verified_minutes"] == 0
