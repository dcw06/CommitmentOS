from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from commitmentos.application.dto import AuthenticatedActor, CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.models import LifecycleStatus
from commitmentos.domain.shared.errors import DomainError
from commitmentos.domain.shared.types import CanonicalEncoder


@dataclass(frozen=True, slots=True)
class CompleteCommitmentRequest:
    commitment_id: str
    idempotency_key: str
    completed_at: datetime
    expected_revision: int
    note: str | None = None


class CompleteCommitment:
    """Explicit manual completion — the only P0 path to `completed` (plan §4.5).

    One transaction writes the completion evidence record, the terminal
    lifecycle transition with `completed_at`, and the continuation
    observation. Verified minutes are never altered: closure below the
    original estimate is honest history, not a gap to fill.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        observation_factory: ObservationFactory,
        observation_dispatcher: ObservationDispatcher,
        clock: Clock,
        activity_factory: ActivityEventFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._observation_factory = observation_factory
        self._observation_dispatcher = observation_dispatcher
        self._clock = clock
        self._activity_factory = activity_factory or ActivityEventFactory()

    async def execute(
        self,
        actor: AuthenticatedActor,
        request: CompleteCommitmentRequest,
        trace_id: str,
    ) -> CommandResult:
        self._validate(request)
        now = self._clock.now()
        evidence_id = CanonicalEncoder.hash(
            [
                "commitment-completion-evidence:v1",
                actor.user_id,
                request.commitment_id,
                request.idempotency_key,
            ]
        )

        async def _complete(repositories: RepositorySet) -> CommandResult:
            existing_evidence = await repositories.evidence.get(evidence_id)
            commitment = await repositories.commitments.get(request.commitment_id)
            if commitment is None:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"commitment_id": request.commitment_id},
                    None,
                    "commitment_not_found",
                )
            if commitment.user_id != actor.user_id:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"commitment_id": request.commitment_id},
                    None,
                    "commitment_forbidden",
                )
            if existing_evidence is not None:
                if not self._same_request(existing_evidence, request):
                    return CommandResult(
                        CommandStatus.TERMINAL_FAILURE,
                        {"commitment_id": request.commitment_id, "evidence_id": evidence_id},
                        commitment.revision,
                        "idempotency_key_reused",
                    )
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"commitment_id": request.commitment_id, "evidence_id": evidence_id},
                    commitment.revision,
                    "completion_already_recorded",
                )
            if commitment.lifecycle_status == LifecycleStatus.COMPLETED:
                # Terminal invariant: a completed commitment stays closed and a
                # second completion act mutates nothing (plan §4.5).
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"commitment_id": request.commitment_id},
                    commitment.revision,
                    "commitment_already_completed",
                )
            if commitment.revision != request.expected_revision:
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"commitment_id": request.commitment_id},
                    commitment.revision,
                    "commitment_revision_conflict",
                )
            completed = commitment.complete(evidence_id, request.completed_at)
            verified_minutes = sum(
                block.verified_minutes
                for block in await repositories.work_blocks.list_for_commitment(
                    request.commitment_id
                )
            )
            evidence = {
                "evidence_id": evidence_id,
                "evidence_type": "commitment_completion",
                "user_id": actor.user_id,
                "commitment_id": request.commitment_id,
                "client_idempotency_key": request.idempotency_key,
                "completed_at": request.completed_at,
                "note": request.note,
                "verified_minutes_at_completion": verified_minutes,
                "confirmed_minutes_at_completion": commitment.effort.confirmed_minutes,
                "recorded_at": now,
                "actor": actor.user_id,
                "created_at": now,
            }
            await repositories.commitments.save(completed, commitment.revision)
            await repositories.evidence.create(evidence)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=ActivityEventType.COMPLETION_RECORDED,
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary=(
                        "Commitment completed with explicit user evidence; "
                        f"{verified_minutes} verified minute(s) retained unchanged"
                    ),
                    payload={
                        "commitment_id": request.commitment_id,
                        "commitment_revision": completed.revision,
                        "evidence_id": evidence_id,
                        "verified_minutes_at_completion": verified_minutes,
                        "confirmed_minutes_at_completion": commitment.effort.confirmed_minutes,
                        "completed_at": request.completed_at.isoformat(),
                    },
                    created_at=now,
                )
            )
            observation = self._observation_factory.continuation(
                observation_type=ObservationType.COMPLETION_CONFIRMED,
                user_id=actor.user_id,
                producer_id=evidence_id,
                producer_version=str(completed.revision),
                safe_metadata={
                    "commitment_id": request.commitment_id,
                    "commitment_revision": completed.revision,
                    "evidence_id": evidence_id,
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            return CommandResult(
                CommandStatus.COMPLETED,
                {
                    "commitment_id": request.commitment_id,
                    "evidence_id": evidence_id,
                    "observation_id": observation.observation_id,
                },
                completed.revision,
                None,
            )

        try:
            result = await self._unit_of_work.run(_complete)
        except DomainError as error:
            return CommandResult(
                CommandStatus.TERMINAL_FAILURE,
                {"commitment_id": request.commitment_id},
                None,
                type(error).__name__,
            )
        if result.status == CommandStatus.COMPLETED:
            observation_id = result.identifiers.get("observation_id")
            if observation_id:
                try:
                    await self._observation_dispatcher.dispatch(observation_id)
                except Exception:  # noqa: BLE001 - dispatcher repair closes the crash gap
                    pass
        return result

    def _validate(self, request: CompleteCommitmentRequest) -> None:
        if not request.idempotency_key or len(request.idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        if request.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if request.note is not None and len(request.note) > 500:
            raise ValueError("note must contain at most 500 characters")
        if request.completed_at.tzinfo is None or request.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        if request.completed_at.astimezone(timezone.utc) > self._clock.now().astimezone(
            timezone.utc
        ):
            raise ValueError("completed_at cannot be in the future")

    @staticmethod
    def _same_request(existing: object, request: CompleteCommitmentRequest) -> bool:
        if not isinstance(existing, dict):
            return False
        return (
            existing.get("commitment_id") == request.commitment_id
            and existing.get("client_idempotency_key") == request.idempotency_key
            and existing.get("completed_at") == request.completed_at
            and existing.get("note") == request.note
        )
