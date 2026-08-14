from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from commitmentos.application.dto import AuthenticatedActor, CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.shared.errors import DomainError
from commitmentos.domain.shared.types import CanonicalEncoder


@dataclass(frozen=True, slots=True)
class WorkCheckInRequest:
    work_block_id: str
    idempotency_key: str
    completed: bool
    verified_minutes: int
    checked_in_at: datetime
    expected_revision: int


class RecordWorkCheckIn:
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
        request: WorkCheckInRequest,
        trace_id: str,
    ) -> CommandResult:
        self._validate(request)
        now = self._clock.now()
        evidence_id = CanonicalEncoder.hash(
            [
                "work-check-in-evidence:v1",
                actor.user_id,
                request.work_block_id,
                request.idempotency_key,
            ]
        )

        async def _record(repositories: RepositorySet) -> CommandResult:
            existing_evidence = await repositories.evidence.get(evidence_id)
            block = await repositories.work_blocks.get(request.work_block_id)
            if block is None:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"work_block_id": request.work_block_id},
                    None,
                    "work_block_not_found",
                )
            commitment = await repositories.commitments.get(block.commitment_id)
            if commitment is None:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"work_block_id": request.work_block_id},
                    None,
                    "commitment_not_found",
                )
            if commitment.user_id != actor.user_id:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"work_block_id": request.work_block_id},
                    None,
                    "work_block_forbidden",
                )
            if existing_evidence is not None:
                if not self._same_request(existing_evidence, request):
                    return CommandResult(
                        CommandStatus.TERMINAL_FAILURE,
                        {"work_block_id": request.work_block_id, "evidence_id": evidence_id},
                        block.revision,
                        "idempotency_key_reused",
                    )
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"work_block_id": request.work_block_id, "evidence_id": evidence_id},
                    block.revision,
                    "check_in_already_recorded",
                )
            if block.revision != request.expected_revision:
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"work_block_id": request.work_block_id},
                    block.revision,
                    "work_block_revision_conflict",
                )
            updated = block.record_check_in(
                completed=request.completed,
                verified_minutes=request.verified_minutes,
                evidence_id=evidence_id,
                at=request.checked_in_at,
            )
            evidence = {
                "evidence_id": evidence_id,
                "evidence_type": "work_check_in",
                "user_id": actor.user_id,
                "commitment_id": block.commitment_id,
                "work_block_id": block.work_block_id,
                "client_idempotency_key": request.idempotency_key,
                "completed": request.completed,
                "verified_minutes": request.verified_minutes,
                "checked_in_at": request.checked_in_at,
                "recorded_at": now,
                "actor": actor.user_id,
                "created_at": now,
            }
            await repositories.work_blocks.save(updated, block.revision)
            await repositories.evidence.create(evidence)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=ActivityEventType.WORK_CHECK_IN_RECORDED,
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary=(
                        f"Work check-in recorded: {request.verified_minutes} verified minute(s)"
                    ),
                    payload={
                        "commitment_id": block.commitment_id,
                        "work_block_id": block.work_block_id,
                        "work_block_revision": updated.revision,
                        "verified_minutes": request.verified_minutes,
                        "completed": request.completed,
                        "evidence_id": evidence_id,
                    },
                    created_at=now,
                )
            )
            observation = self._observation_factory.continuation(
                observation_type=ObservationType.WORK_CHECK_IN_RECORDED,
                user_id=actor.user_id,
                producer_id=evidence_id,
                producer_version=str(updated.revision),
                safe_metadata={
                    "commitment_id": block.commitment_id,
                    "work_block_id": block.work_block_id,
                    "work_block_revision": updated.revision,
                    "evidence_id": evidence_id,
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            return CommandResult(
                CommandStatus.COMPLETED,
                {
                    "work_block_id": block.work_block_id,
                    "commitment_id": block.commitment_id,
                    "evidence_id": evidence_id,
                    "observation_id": observation.observation_id,
                },
                updated.revision,
                None,
            )

        try:
            result = await self._unit_of_work.run(_record)
        except DomainError as error:
            return CommandResult(
                CommandStatus.TERMINAL_FAILURE,
                {"work_block_id": request.work_block_id},
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

    def _validate(self, request: WorkCheckInRequest) -> None:
        if not request.idempotency_key or len(request.idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        if request.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if request.verified_minutes < 0:
            raise ValueError("verified_minutes cannot be negative")
        if request.checked_in_at.tzinfo is None or request.checked_in_at.utcoffset() is None:
            raise ValueError("checked_in_at must be timezone-aware")
        if request.checked_in_at.astimezone(timezone.utc) > self._clock.now().astimezone(
            timezone.utc
        ):
            raise ValueError("checked_in_at cannot be in the future")

    @staticmethod
    def _same_request(existing: object, request: WorkCheckInRequest) -> bool:
        if not isinstance(existing, dict):
            return False
        return (
            existing.get("work_block_id") == request.work_block_id
            and existing.get("client_idempotency_key") == request.idempotency_key
            and existing.get("completed") is request.completed
            and existing.get("verified_minutes") == request.verified_minutes
            and existing.get("checked_in_at") == request.checked_in_at
        )
