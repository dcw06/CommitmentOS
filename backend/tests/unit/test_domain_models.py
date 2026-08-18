from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from commitmentos.application.services.portfolio_planning import PortfolioPlanningService
from commitmentos.contracts.observations import ObservationIdFactory, ObservationType
from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    ReconcileObservationTaskV1,
    TaskNameFactory,
)
from commitmentos.domain.actions.models import (
    ActionOutbox,
    CalendarActionType,
    CalendarMutation,
    DispatchStatus,
    ExecutionStatus,
    MutationResponseEvidence,
)
from commitmentos.domain.commitments.models import (
    BlockingStatus,
    Commitment,
    CommitmentProjection,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
    RiskLevel,
)
from commitmentos.domain.controls.models import ControlMode, initial_system_controls
from commitmentos.domain.planning.models import TimeInterval
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)
from commitmentos.domain.shared.errors import InvalidTransitionError
from commitmentos.domain.shared.types import CanonicalEncoder
from commitmentos.workflows.reconciliation.phase1_workflow import derive_calendar_event_id

NOW = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)


def make_commitment(**overrides) -> Commitment:
    defaults = dict(
        commitment_id="c1",
        user_id="u1",
        revision=1,
        source_thread_id="t1",
        semantic_fingerprint="fp",
        title="Send revised proposal",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner={"type": "user"},
        beneficiary={"display_name": "Professor Chen"},
        deadline=Deadline(
            value=NOW + timedelta(days=2),
            timezone="America/Los_Angeles",
            confidence=0.93,
            evidence_id="e1",
            source_expression="Friday 4 p.m.",
            rule_version="v1",
        ),
        effort=Effort(proposed_minutes=180, confidence=0.5, confirmed_minutes=None, confirmed_at=None),
        lifecycle_status=LifecycleStatus.AWAITING_CONFIRMATION,
        completion_evidence_id=None,
        completed_at=None,
        plan_revision=0,
        projection=None,
        policy_profile="default_personal",
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return Commitment(**defaults)


def make_outbox(**overrides) -> ActionOutbox:
    defaults = dict(
        outbox_id="o1",
        user_id="u1",
        commitment_id="c1",
        work_block_id="b1",
        action_idempotency_key="action:c1:1:insert:b1",
        expected_commitment_revision=2,
        expected_plan_revision=1,
        expected_projection_hash="",
        expected_control_epoch=1,
        before_state=None,
        mutation=CalendarMutation(
            action_type=CalendarActionType.INSERT,
            calendar_id="primary",
            calendar_event_id="evt",
            work_block_id="b1",
            desired_start=NOW,
            desired_end=NOW + timedelta(hours=1),
            expected_observed_event_etag=None,
            private_properties={},
        ),
        dispatch_status=DispatchStatus.PENDING,
        execution_status=ExecutionStatus.PENDING,
        claim_token=None,
        claim_lease_expires_at=None,
        attempts=0,
        mutation_response=None,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return ActionOutbox(**defaults)


class TestCanonicalEncoding:
    def test_length_delimiting_prevents_concatenation_collisions(self) -> None:
        assert CanonicalEncoder.hash(["ab", "c"]) != CanonicalEncoder.hash(["a", "bc"])

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            CanonicalEncoder.encode([datetime(2026, 1, 1)])

    def test_observation_id_is_deterministic(self) -> None:
        factory = ObservationIdFactory()
        first = factory.create(ObservationType.ACTION_RESULT, "outbox-1", "succeeded")
        second = factory.create(ObservationType.ACTION_RESULT, "outbox-1", "succeeded")
        assert first == second
        different_type = factory.create(
            ObservationType.APPROVAL_RESOLVED, "outbox-1", "succeeded"
        )
        assert first != different_type


class TestTaskNames:
    def test_reconciliation_name_changes_with_dispatch_generation(self) -> None:
        factory = TaskNameFactory()
        base = ReconcileObservationTaskV1("v1", "obs1", "wf1", 0, "trace")
        bumped = ReconcileObservationTaskV1("v1", "obs1", "wf1", 1, "trace")
        assert factory.reconciliation(base) != factory.reconciliation(bumped)
        assert factory.reconciliation(base) == factory.reconciliation(base)

    def test_calendar_action_name_is_stable_per_intent(self) -> None:
        factory = TaskNameFactory()
        task = ExecuteCalendarActionTaskV1("v1", "outbox1", "key1", "trace")
        assert factory.calendar_action(task) == factory.calendar_action(task)


class TestCalendarEventIdentity:
    def test_derived_id_is_base32hex_lowercase_without_padding(self) -> None:
        event_id = derive_calendar_event_id("primary", "block-1")
        assert event_id == event_id.lower()
        assert "=" not in event_id
        assert 5 <= len(event_id) <= 1024
        assert all(c in "0123456789abcdefghijklmnopqrstuv" for c in event_id)

    def test_id_excludes_mutable_values(self) -> None:
        # Identity depends only on calendar and immutable work-block identity.
        assert derive_calendar_event_id("primary", "block-1") == derive_calendar_event_id(
            "primary", "block-1"
        )
        assert derive_calendar_event_id("primary", "block-1") != derive_calendar_event_id(
            "primary", "block-2"
        )


class TestCommitmentLifecycle:
    def test_completion_requires_explicit_evidence(self) -> None:
        commitment = make_commitment(
            lifecycle_status=LifecycleStatus.ACTIVE,
            projection=CommitmentProjection(
                verified_completed_minutes=60,
                remaining_minutes=120,
                risk_level=RiskLevel.ON_TRACK,
                blocking_status=BlockingStatus.CLEAR,
                source_commitment_revision=1,
                source_work_block_revision_hash="blocks-v1",
                planner_run_id="plan-v1",
                calculator_version="projection-v1",
                computed_at=NOW,
            ),
        )
        with pytest.raises(InvalidTransitionError):
            commitment.transition(LifecycleStatus.COMPLETED, NOW)
        completed = commitment.complete("evidence-1", NOW)
        assert completed.lifecycle_status == LifecycleStatus.COMPLETED
        assert completed.completion_evidence_id == "evidence-1"
        assert completed.projection is None
        assert completed.revision == commitment.revision + 1

    def test_completed_is_terminal_for_reconciliation(self) -> None:
        completed = make_commitment(lifecycle_status=LifecycleStatus.ACTIVE).complete("e", NOW)
        with pytest.raises(InvalidTransitionError):
            completed.transition(LifecycleStatus.ACTIVE, NOW)
        with pytest.raises(InvalidTransitionError):
            completed.complete("e2", NOW)

    def test_reopen_is_an_explicit_new_revision(self) -> None:
        completed = make_commitment(lifecycle_status=LifecycleStatus.ACTIVE).complete("e", NOW)
        reopened = completed.reopen(NOW)
        assert reopened.lifecycle_status == LifecycleStatus.ACTIVE
        assert reopened.completion_evidence_id is None
        assert reopened.revision == completed.revision + 1

    def test_confirm_effort_bumps_revision(self) -> None:
        confirmed = make_commitment().confirm_effort(180, NOW)
        assert confirmed.effort.confirmed_minutes == 180
        assert confirmed.revision == 2
        with pytest.raises(InvalidTransitionError):
            make_commitment().confirm_effort(0, NOW)


class TestWorkBlockProgress:
    def make_block(self, **overrides) -> WorkBlock:
        defaults = dict(
            work_block_id="b1",
            commitment_id="c1",
            revision=1,
            calendar_id="primary",
            calendar_event_id="evt",
            calendar_snapshot_id=None,
            duration_minutes=60,
            execution_state=WorkBlockExecutionState.PLANNED,
            scheduled_start=NOW,
            scheduled_end=NOW + timedelta(hours=1),
            verified_minutes=0,
            completion_evidence_id=None,
            user_edit_state=UserEditState.NONE,
            plan_revision=1,
        )
        defaults.update(overrides)
        return WorkBlock(**defaults)

    def test_elapsed_time_never_reduces_remaining_effort(self) -> None:
        # An elapsed block asks for a check-in; it never invents minutes.
        block = self.make_block(execution_state=WorkBlockExecutionState.ACTIVE)
        awaiting = block.request_check_in(NOW)
        assert awaiting.execution_state == WorkBlockExecutionState.AWAITING_CHECK_IN
        assert awaiting.verified_minutes == 0

    def test_check_in_bounded_by_duration(self) -> None:
        block = self.make_block(execution_state=WorkBlockExecutionState.AWAITING_CHECK_IN)
        with pytest.raises(InvalidTransitionError):
            block.check_in(61, "e", NOW)
        checked = block.check_in(60, "e", NOW)
        assert checked.verified_minutes == 60
        assert checked.execution_state == WorkBlockExecutionState.COMPLETED

    def test_only_planned_blocks_cancel(self) -> None:
        with pytest.raises(InvalidTransitionError):
            self.make_block(execution_state=WorkBlockExecutionState.COMPLETED).cancel(NOW)


class TestOutboxStateMachine:
    def test_pending_patch_before_state_remains_busy_for_planning(self) -> None:
        base = make_outbox()
        action = replace(
            base,
            mutation=replace(base.mutation, action_type=CalendarActionType.PATCH),
            before_state={
                "scheduled_start": NOW.isoformat(),
                "scheduled_end": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
        busy = PortfolioPlanningService._pending_action_busy(
            (action,),
            TimeInterval(NOW - timedelta(hours=1), NOW + timedelta(hours=2)),
        )
        assert len(busy) == 1
        assert busy[0].interval == TimeInterval(NOW, NOW + timedelta(hours=1))
        assert busy[0].work_block_id is None

    def test_pending_adoption_does_not_reserve_superseded_plan_interval(self) -> None:
        base = make_outbox()
        action = replace(
            base,
            mutation=replace(base.mutation, action_type=CalendarActionType.ADOPT),
            before_state={
                "scheduled_start": NOW.isoformat(),
                "scheduled_end": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
        assert PortfolioPlanningService._pending_action_busy(
            (action,),
            TimeInterval(NOW - timedelta(hours=1), NOW + timedelta(hours=2)),
        ) == ()

    def test_external_io_only_from_claimed(self) -> None:
        action = make_outbox()
        with pytest.raises(InvalidTransitionError):
            action.start_external_io(NOW)
        claimed = action.claim("tok", NOW + timedelta(minutes=2), NOW)
        in_flight = claimed.start_external_io(NOW)
        assert in_flight.execution_status == ExecutionStatus.ACTION_IN_FLIGHT
        assert in_flight.attempts == 1

    def test_unexpired_claim_cannot_be_taken_over(self) -> None:
        claimed = make_outbox().claim("tok", NOW + timedelta(minutes=2), NOW)
        with pytest.raises(InvalidTransitionError):
            claimed.claim("tok2", NOW + timedelta(minutes=4), NOW + timedelta(seconds=30))
        takeover = claimed.claim("tok2", NOW + timedelta(minutes=5), NOW + timedelta(minutes=3))
        assert takeover.claim_token == "tok2"

    def test_412_is_terminal_stale_precondition(self) -> None:
        in_flight = (
            make_outbox().claim("tok", NOW + timedelta(minutes=2), NOW).start_external_io(NOW)
        )
        stale = in_flight.fail_stale_precondition({"error_code": "failedPrecondition"}, NOW)
        assert stale.execution_status == ExecutionStatus.STALE_PRECONDITION
        with pytest.raises(InvalidTransitionError):
            stale.start_external_io(NOW)
        with pytest.raises(InvalidTransitionError):
            stale.supersede(NOW)

    def test_hold_preserves_intent(self) -> None:
        held = make_outbox().hold(3, NOW)
        assert held.execution_status == ExecutionStatus.HELD_BY_CONTROL
        assert held.dispatch_status == DispatchStatus.HELD_BY_CONTROL
        assert held.mutation.action_type == CalendarActionType.INSERT

    def test_succeed_records_mutation_response_evidence(self) -> None:
        in_flight = (
            make_outbox().claim("tok", NOW + timedelta(minutes=2), NOW).start_external_io(NOW)
        )
        response = MutationResponseEvidence(
            mutation_response_etag='"etag-1"',
            mutation_response_payload_hash="hash",
            mutation_response_status="applied",
            mutation_response_received_at=NOW,
        )
        succeeded = in_flight.succeed(response, NOW)
        assert succeeded.execution_status == ExecutionStatus.SUCCEEDED
        assert succeeded.mutation_response is response


class TestSystemControls:
    def test_every_change_increments_the_epoch(self) -> None:
        controls = initial_system_controls("u1", NOW)
        assert controls.control_epoch == 1
        paused = controls.set_automatic_action_mode(ControlMode.PAUSED, "u1", "test", NOW)
        assert paused.control_epoch == 2
        assert not paused.allows_automatic_actions()
        assert paused.allows_observation_processing()
        resumed = paused.set_automatic_action_mode(ControlMode.ENABLED, "u1", "test", NOW)
        assert resumed.control_epoch == 3
