from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from commitmentos.application.dto import CommandResult, CommandStatus
from commitmentos.application.ports.calendar_reader import CalendarReader
from commitmentos.application.ports.calendar_writer import (
    CalendarMutationOutcome,
    CalendarMutationOutcomeType,
    CalendarWriter,
)
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.application.services.projection_guard import projection_hash
from commitmentos.application.services.source_sync_dispatcher import SourceSyncDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.contracts.tasks import ExecuteCalendarActionTaskV1, SourceType
from commitmentos.domain.actions.models import (
    TERMINAL_EXECUTION_STATUSES,
    ActionOutbox,
    CalendarActionType,
    CalendarMutation,
    DispatchStatus,
    ExecutionStatus,
    MutationResponseEvidence,
)
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.models import Commitment
from commitmentos.domain.progress.service import ProgressCalculator

CLAIM_LEASE_SECONDS = 120
MAX_CALENDAR_ACTION_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class ActionClaim:
    action: ActionOutbox
    claimed_control_epoch: int
    trace_id: str


@dataclass(frozen=True, slots=True)
class _PreflightHalt:
    result: CommandResult


class ExecuteCalendarAction:
    """The single authoritative external-action path.

    Two transactions guard every Calendar request: an initial claim that
    verifies revisions, controls, and a fenced claim lease, and a final
    pre-I/O transaction that rechecks the same guards and moves
    `claimed -> action_in_flight` — the external-I/O linearization point.
    A 412 marks the intent stale, requests synchronization, and never
    retries with a replacement etag.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        calendar_reader: CalendarReader,
        calendar_writer: CalendarWriter,
        observation_factory: ObservationFactory,
        observation_dispatcher: ObservationDispatcher,
        clock: Clock,
        id_generator: IdGenerator,
        activity_factory: ActivityEventFactory | None = None,
        source_sync_dispatcher: SourceSyncDispatcher | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._calendar_reader = calendar_reader
        self._calendar_writer = calendar_writer
        self._observation_factory = observation_factory
        self._observation_dispatcher = observation_dispatcher
        self._clock = clock
        self._id_generator = id_generator
        self._activity_factory = activity_factory or ActivityEventFactory()
        self._source_sync_dispatcher = source_sync_dispatcher
        self._progress_calculator = ProgressCalculator()

    async def execute(self, task: ExecuteCalendarActionTaskV1) -> CommandResult:
        claim_or_halt = await self._initial_claim(task)
        if isinstance(claim_or_halt, _PreflightHalt):
            return claim_or_halt.result
        claim = claim_or_halt
        if claim.action.execution_status == ExecutionStatus.ACTION_IN_FLIGHT:
            outcome = await self._recover_action_in_flight(claim)
        else:
            preflight = await self._final_preflight(claim)
            if isinstance(preflight, _PreflightHalt):
                return preflight.result
            outcome = await self._execute_mutation(preflight.mutation)
            claim = ActionClaim(
                action=preflight,
                claimed_control_epoch=claim.claimed_control_epoch,
                trace_id=claim.trace_id,
            )
        if outcome.outcome_type == CalendarMutationOutcomeType.RETRYABLE_FAILURE:
            if claim.action.attempts >= MAX_CALENDAR_ACTION_ATTEMPTS:
                exhausted = CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                    event=None,
                    error=dict(outcome.error or {})
                    | {
                        "error_code": "calendar_action_retry_exhausted",
                        "attempts": claim.action.attempts,
                    },
                )
                return await self._record_terminal_outcome(
                    claim.action,
                    exhausted,
                    claim.trace_id,
                )
            return await self._record_retryable_failure(claim.action, outcome)
        if outcome.outcome_type == CalendarMutationOutcomeType.PRECONDITION_FAILED:
            return await self._record_stale_precondition(claim.action, outcome, claim.trace_id)
        return await self._record_terminal_outcome(claim.action, outcome, claim.trace_id)

    async def _initial_claim(
        self,
        task: ExecuteCalendarActionTaskV1,
    ) -> ActionClaim | _PreflightHalt:
        now = self._clock.now()
        claim_token = self._id_generator.new_token(16)

        async def _claim(repositories: RepositorySet) -> ActionClaim | _PreflightHalt:
            action = await repositories.outbox.get(task.outbox_id)
            if action is None:
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.TERMINAL_FAILURE,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code="outbox_not_found",
                    )
                )
            if action.action_idempotency_key != task.action_idempotency_key:
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.TERMINAL_FAILURE,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code="idempotency_key_mismatch",
                    )
                )
            if action.execution_status in TERMINAL_EXECUTION_STATUSES:
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code=None,
                    )
                )
            if action.execution_status == ExecutionStatus.ACTION_IN_FLIGHT:
                # A previous worker died after the linearization point;
                # recovery inspects external truth before anything else.
                return ActionClaim(
                    action=action,
                    claimed_control_epoch=action.expected_control_epoch,
                    trace_id=task.trace_id,
                )
            calendar_cursor = await repositories.sync_cursors.get(
                action.user_id, SourceType.CALENDAR
            )
            if calendar_cursor is not None and (
                calendar_cursor.publish_in_progress_generation_id is not None
                or calendar_cursor.full_resync_required
            ):
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.RETRYABLE_FAILURE,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code="calendar_truth_ineligible",
                    )
                )
            controls = await repositories.system_controls.get(action.user_id)
            if not controls.allows_automatic_actions():
                if action.execution_status != ExecutionStatus.HELD_BY_CONTROL:
                    held = action.hold(controls.control_epoch, now)
                    await repositories.outbox.save(held, action.updated_at)
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.HELD,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code=None,
                    )
                )
            if action.execution_status == ExecutionStatus.HELD_BY_CONTROL:
                # Held records are released only through resume revalidation.
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.HELD,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code=None,
                    )
                )
            if controls.control_epoch != action.expected_control_epoch:
                stale = action.mark_stale("control_epoch_changed", now)
                await repositories.outbox.save(stale, action.updated_at)
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code="action_stale",
                    )
                )
            commitment = await repositories.commitments.get(action.commitment_id)
            projection_is_current = await self._projection_is_current(
                repositories,
                commitment,
                action.expected_projection_hash,
            )
            if (
                commitment is None
                or commitment.revision != action.expected_commitment_revision
                or commitment.plan_revision != action.expected_plan_revision
                or not projection_is_current
            ):
                stale = action.mark_stale("revision_changed", now)
                await repositories.outbox.save(stale, action.updated_at)
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": task.outbox_id},
                        revision=None,
                        error_code="action_stale",
                    )
                )
            stored_updated_at = action.updated_at
            if action.dispatch_status == DispatchStatus.QUEUED:
                action = action.mark_delivered(now)
            claimed = action.claim(
                claim_token,
                now + timedelta(seconds=CLAIM_LEASE_SECONDS),
                now,
            )
            await repositories.outbox.save(claimed, stored_updated_at)
            return ActionClaim(
                action=claimed,
                claimed_control_epoch=controls.control_epoch,
                trace_id=task.trace_id,
            )

        return await self._unit_of_work.run(_claim)

    async def _recover_action_in_flight(self, claim: ActionClaim) -> CalendarMutationOutcome:
        """Converge after a worker died beyond the linearization point.

        For inserts, the stable client-supplied event ID makes external truth
        inspectable: an existing owned event is the completed mutation. Other
        action types return a retryable failure so Cloud Tasks retries after
        the intervening state is observable.
        """
        mutation = claim.action.mutation
        if mutation.action_type in (CalendarActionType.INSERT, CalendarActionType.ADOPT):
            return await self._calendar_writer.insert_or_adopt_owned(mutation)
        event = await self._calendar_reader.get_event(
            mutation.calendar_id, mutation.calendar_event_id
        )
        if event is None and mutation.action_type == CalendarActionType.CANCEL:
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.ALREADY_APPLIED,
                event=None,
                error=None,
            )
        return CalendarMutationOutcome(
            outcome_type=CalendarMutationOutcomeType.RETRYABLE_FAILURE,
            event=event,
            error={"error_code": "in_flight_recovery_requires_synchronized_truth"},
        )

    async def _final_preflight(self, claim: ActionClaim) -> ActionOutbox | _PreflightHalt:
        now = self._clock.now()

        async def _preflight(repositories: RepositorySet) -> ActionOutbox | _PreflightHalt:
            action = await repositories.outbox.get(claim.action.outbox_id)
            if action is None or action.execution_status != ExecutionStatus.CLAIMED:
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": claim.action.outbox_id},
                        revision=None,
                        error_code="claim_lost",
                    )
                )
            if action.claim_token != claim.action.claim_token:
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": claim.action.outbox_id},
                        revision=None,
                        error_code="claim_taken_over",
                    )
                )
            calendar_cursor = await repositories.sync_cursors.get(
                action.user_id, SourceType.CALENDAR
            )
            if calendar_cursor is not None and (
                calendar_cursor.publish_in_progress_generation_id is not None
                or calendar_cursor.full_resync_required
            ):
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.RETRYABLE_FAILURE,
                        identifiers={"outbox_id": claim.action.outbox_id},
                        revision=None,
                        error_code="calendar_truth_ineligible",
                    )
                )
            controls = await repositories.system_controls.get(action.user_id)
            if (
                not controls.allows_automatic_actions()
                or controls.control_epoch != claim.claimed_control_epoch
            ):
                held = action.hold(controls.control_epoch, now)
                await repositories.outbox.save(held, action.updated_at)
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.HELD,
                        identifiers={"outbox_id": claim.action.outbox_id},
                        revision=None,
                        error_code=None,
                    )
                )
            commitment = await repositories.commitments.get(action.commitment_id)
            projection_is_current = await self._projection_is_current(
                repositories,
                commitment,
                action.expected_projection_hash,
            )
            if (
                commitment is None
                or commitment.revision != action.expected_commitment_revision
                or commitment.plan_revision != action.expected_plan_revision
                or not projection_is_current
            ):
                stale = action.mark_stale("revision_changed", now)
                await repositories.outbox.save(stale, action.updated_at)
                return _PreflightHalt(
                    CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"outbox_id": claim.action.outbox_id},
                        revision=None,
                        error_code="action_stale",
                    )
                )
            if action.mutation.action_type in (CalendarActionType.PATCH, CalendarActionType.CANCEL):
                if action.mutation.expected_observed_event_etag is None:
                    stale = action.mark_stale("missing_expected_etag", now)
                    await repositories.outbox.save(stale, action.updated_at)
                    return _PreflightHalt(
                        CommandResult(
                            status=CommandStatus.NO_OP,
                            identifiers={"outbox_id": claim.action.outbox_id},
                            revision=None,
                            error_code="action_stale",
                        )
                    )
                snapshot = await repositories.calendar_snapshots.get(
                    action.mutation.calendar_id, action.mutation.calendar_event_id
                )
                if (
                    snapshot is None
                    or snapshot.observed_event_etag
                    != action.mutation.expected_observed_event_etag
                ):
                    stale = action.mark_stale("observed_etag_changed", now)
                    await repositories.outbox.save(stale, action.updated_at)
                    return _PreflightHalt(
                        CommandResult(
                            status=CommandStatus.NO_OP,
                            identifiers={"outbox_id": claim.action.outbox_id},
                            revision=None,
                            error_code="action_stale",
                        )
                    )
            in_flight = action.start_external_io(now)
            await repositories.outbox.save(in_flight, action.updated_at)
            return in_flight

        return await self._unit_of_work.run(_preflight)

    async def _projection_is_current(
        self,
        repositories: RepositorySet,
        commitment: Commitment | None,
        expected_projection_hash: str,
    ) -> bool:
        # Phase 1 outbox fixtures predate projections; only provenance-bearing
        # Phase 3 actions opt into this additional guard.
        if not expected_projection_hash:
            return True
        if commitment is None or commitment.projection is None:
            return False
        work_blocks = await repositories.work_blocks.list_for_commitment(
            commitment.commitment_id
        )
        current_revision_hash = self._progress_calculator.work_block_revision_hash(
            work_blocks
        )
        return (
            commitment.projection.matches(commitment.revision, current_revision_hash)
            and projection_hash(commitment) == expected_projection_hash
        )

    async def _execute_mutation(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        if mutation.action_type in (CalendarActionType.INSERT, CalendarActionType.ADOPT):
            return await self._calendar_writer.insert_or_adopt_owned(mutation)
        if mutation.action_type == CalendarActionType.PATCH:
            return await self._calendar_writer.patch_owned(mutation)
        return await self._calendar_writer.cancel_owned(mutation)

    async def _record_terminal_outcome(
        self,
        action: ActionOutbox,
        outcome: CalendarMutationOutcome,
        trace_id: str,
    ) -> CommandResult:
        now = self._clock.now()
        succeeded = outcome.outcome_type in (
            CalendarMutationOutcomeType.APPLIED,
            CalendarMutationOutcomeType.ALREADY_APPLIED,
        )
        response = MutationResponseEvidence(
            mutation_response_etag=outcome.event.etag if outcome.event is not None else None,
            mutation_response_payload_hash=(
                outcome.event.payload_hash if outcome.event is not None else ""
            ),
            mutation_response_status=outcome.outcome_type.value,
            mutation_response_received_at=now,
        )

        async def _record(repositories: RepositorySet) -> CommandResult:
            current = await repositories.outbox.get(action.outbox_id)
            if current is None or current.execution_status != ExecutionStatus.ACTION_IN_FLIGHT:
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"outbox_id": action.outbox_id},
                    revision=None,
                    error_code="outcome_already_recorded",
                )
            if succeeded:
                terminal = current.succeed(response, now)
            else:
                terminal = current.fail_terminally(dict(outcome.error or {}), now)
            await repositories.outbox.save(terminal, current.updated_at)
            source_observation_id = ""
            for event in await repositories.activity.list_for_user(
                action.user_id, None, 250
            ):
                if action.outbox_id in tuple(event.payload.get("outbox_ids") or ()):
                    source_observation_id = str(
                        event.payload.get("source_observation_id") or ""
                    )
                    if source_observation_id:
                        break
            source_observation = (
                await repositories.observations.get(source_observation_id)
                if source_observation_id
                else None
            )
            repair_latency_ms = (
                max(
                    0,
                    int(
                        (now - source_observation.observed_at).total_seconds()
                        * 1000
                    ),
                )
                if source_observation is not None
                else None
            )
            observation = self._observation_factory.continuation(
                observation_type=ObservationType.ACTION_RESULT,
                user_id=action.user_id,
                producer_id=action.outbox_id,
                producer_version=terminal.execution_status.value,
                safe_metadata={
                    "outbox_id": action.outbox_id,
                    "commitment_id": action.commitment_id,
                    "work_block_id": action.work_block_id,
                    "action_type": action.mutation.action_type.value,
                    "execution_status": terminal.execution_status.value,
                    "calendar_event_id": action.mutation.calendar_event_id,
                    "source_observation_id": source_observation_id,
                    "repair_latency_ms": repair_latency_ms,
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=action.user_id,
                    event_type=ActivityEventType.CALENDAR_ACTION_RESULT,
                    trace_id=trace_id,
                    actor="calendar_executor",
                    summary=(
                        f"Calendar {action.mutation.action_type.value} "
                        f"{'succeeded' if succeeded else 'failed terminally'}"
                    ),
                    payload={
                        "outbox_id": action.outbox_id,
                        "work_block_id": action.work_block_id,
                        "calendar_event_id": action.mutation.calendar_event_id,
                        "execution_status": terminal.execution_status.value,
                        "observation_id": observation.observation_id,
                        "source_observation_id": source_observation_id,
                        "repair_latency_ms": repair_latency_ms,
                        "warmed_repair_under_15_seconds": (
                            repair_latency_ms is not None
                            and repair_latency_ms < 15_000
                        ),
                    },
                    created_at=now,
                )
            )
            return CommandResult(
                status=CommandStatus.COMPLETED,
                identifiers={
                    "outbox_id": action.outbox_id,
                    "observation_id": observation.observation_id,
                },
                revision=None,
                error_code=None if succeeded else "calendar_action_failed",
            )

        result = await self._unit_of_work.run(_record)
        if result.status == CommandStatus.COMPLETED:
            observation_id = result.identifiers.get("observation_id")
            if observation_id:
                try:
                    await self._observation_dispatcher.dispatch(observation_id)
                except Exception:  # noqa: BLE001 - dispatch is repairable
                    pass
        return result

    async def _record_retryable_failure(
        self,
        action: ActionOutbox,
        outcome: CalendarMutationOutcome,
    ) -> CommandResult:
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            current = await repositories.outbox.get(action.outbox_id)
            if current is None or current.execution_status not in (
                ExecutionStatus.ACTION_IN_FLIGHT,
                ExecutionStatus.CLAIMED,
            ):
                return
            failed = current.fail_retryably(dict(outcome.error or {}), now)
            await repositories.outbox.save(failed, current.updated_at)

        await self._unit_of_work.run(_record)
        # No action_result observation and no reconciliation dispatch for a
        # retryable failure; Cloud Tasks retries the named task.
        return CommandResult(
            status=CommandStatus.RETRYABLE_FAILURE,
            identifiers={"outbox_id": action.outbox_id},
            revision=None,
            error_code=(outcome.error or {}).get("error_code", "calendar_retryable_failure"),
        )

    async def _record_stale_precondition(
        self,
        action: ActionOutbox,
        outcome: CalendarMutationOutcome,
        trace_id: str,
    ) -> CommandResult:
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            current = await repositories.outbox.get(action.outbox_id)
            if current is None or current.execution_status != ExecutionStatus.ACTION_IN_FLIGHT:
                return
            stale = current.fail_stale_precondition(dict(outcome.error or {}), now)
            await repositories.outbox.save(stale, current.updated_at)
            await repositories.sync_requests.upsert(
                f"calendar:{action.user_id}",
                {
                    "source": "calendar",
                    "user_id": action.user_id,
                    "status": "pending",
                    "reason": "stale_precondition",
                    "sync_generation_id": f"stale-{action.outbox_id}",
                    "trace_id": trace_id,
                    "requested_at": now,
                },
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=action.user_id,
                    event_type=ActivityEventType.FAILURE_RECORDED,
                    trace_id=trace_id,
                    actor="calendar_executor",
                    summary="Calendar precondition failed; intent is stale and synchronization was requested",
                    payload={
                        "outbox_id": action.outbox_id,
                        "work_block_id": action.work_block_id,
                        "calendar_event_id": action.mutation.calendar_event_id,
                        "failure": "calendar_precondition_stale",
                    },
                    created_at=now,
                )
            )

        await self._unit_of_work.run(_record)
        if self._source_sync_dispatcher is not None:
            try:
                await self._source_sync_dispatcher.dispatch(
                    f"calendar:{action.user_id}"
                )
            except Exception:  # noqa: BLE001 - maintenance repairs dispatch gaps
                pass
        # Acknowledge the task: a 412 is not a retryable infrastructure error
        # and must never be retried with a freshly fetched etag.
        return CommandResult(
            status=CommandStatus.COMPLETED,
            identifiers={"outbox_id": action.outbox_id},
            revision=None,
            error_code="calendar_precondition_stale",
        )
