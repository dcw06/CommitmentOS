from __future__ import annotations

from dataclasses import dataclass

from commitmentos.application.dto import AuthenticatedActor, CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.models import LifecycleStatus
from commitmentos.domain.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class ReopenCommitmentRequest:
    commitment_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class SetCommitmentPriorityRequest:
    commitment_id: str
    expected_revision: int
    priority: int


@dataclass(frozen=True, slots=True)
class ChangeCommitmentLifecycleRequest:
    commitment_id: str
    expected_revision: int
    target_status: LifecycleStatus


class ChangeCommitment:
    """Controlled-user corrections that must trigger a fresh portfolio run."""

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

    async def reopen(
        self,
        actor: AuthenticatedActor,
        request: ReopenCommitmentRequest,
        trace_id: str,
    ) -> CommandResult:
        return await self._change(
            actor,
            request.commitment_id,
            request.expected_revision,
            trace_id,
            operation="reopen",
            priority=None,
            lifecycle_target=None,
        )

    async def set_priority(
        self,
        actor: AuthenticatedActor,
        request: SetCommitmentPriorityRequest,
        trace_id: str,
    ) -> CommandResult:
        if not -100 <= request.priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        return await self._change(
            actor,
            request.commitment_id,
            request.expected_revision,
            trace_id,
            operation="set_priority",
            priority=request.priority,
            lifecycle_target=None,
        )

    async def change_lifecycle(
        self,
        actor: AuthenticatedActor,
        request: ChangeCommitmentLifecycleRequest,
        trace_id: str,
    ) -> CommandResult:
        if request.target_status not in (
            LifecycleStatus.PAUSED,
            LifecycleStatus.ACTIVE,
            LifecycleStatus.DISMISSED,
        ):
            raise ValueError("target_status must be paused, active, or dismissed")
        return await self._change(
            actor,
            request.commitment_id,
            request.expected_revision,
            trace_id,
            operation="change_lifecycle",
            priority=None,
            lifecycle_target=request.target_status,
        )

    async def _change(
        self,
        actor: AuthenticatedActor,
        commitment_id: str,
        expected_revision: int,
        trace_id: str,
        *,
        operation: str,
        priority: int | None,
        lifecycle_target: LifecycleStatus | None,
    ) -> CommandResult:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        now = self._clock.now()

        async def _apply(repositories: RepositorySet) -> CommandResult:
            commitment = await repositories.commitments.get(commitment_id)
            if commitment is None:
                return self._failure(commitment_id, "commitment_not_found")
            if commitment.user_id != actor.user_id:
                return self._failure(commitment_id, "commitment_forbidden")
            if commitment.revision != expected_revision:
                return CommandResult(
                    CommandStatus.NO_OP,
                    {"commitment_id": commitment_id},
                    commitment.revision,
                    "commitment_revision_conflict",
                )
            if operation == "reopen":
                if commitment.lifecycle_status != LifecycleStatus.COMPLETED:
                    return CommandResult(
                        CommandStatus.NO_OP,
                        {"commitment_id": commitment_id},
                        commitment.revision,
                        "commitment_not_completed",
                    )
                updated = commitment.reopen(now)
                observation_type = ObservationType.COMMITMENT_REOPENED
                event_type = ActivityEventType.COMMITMENT_REOPENED
                summary = "Completed commitment reopened by the controlled user"
                details = {"previous_status": LifecycleStatus.COMPLETED.value}
            elif operation == "set_priority":
                assert priority is not None
                updated = commitment.reprioritize(priority, now)
                if updated is commitment:
                    return CommandResult(
                        CommandStatus.NO_OP,
                        {"commitment_id": commitment_id},
                        commitment.revision,
                        "priority_unchanged",
                    )
                observation_type = ObservationType.COMMITMENT_PRIORITY_CHANGED
                event_type = ActivityEventType.COMMITMENT_PRIORITY_CHANGED
                summary = "Commitment priority changed by the controlled user"
                details = {
                    "previous_priority": commitment.explicit_priority,
                    "priority": priority,
                }
            else:
                assert lifecycle_target is not None
                if (
                    lifecycle_target == LifecycleStatus.ACTIVE
                    and commitment.lifecycle_status != LifecycleStatus.PAUSED
                ):
                    return CommandResult(
                        CommandStatus.NO_OP,
                        {"commitment_id": commitment_id},
                        commitment.revision,
                        "commitment_not_paused",
                    )
                updated = commitment.transition(lifecycle_target, now)
                if updated is commitment:
                    return CommandResult(
                        CommandStatus.NO_OP,
                        {"commitment_id": commitment_id},
                        commitment.revision,
                        "lifecycle_unchanged",
                    )
                event_map = {
                    LifecycleStatus.PAUSED: (
                        ObservationType.COMMITMENT_PAUSED,
                        ActivityEventType.COMMITMENT_PAUSED,
                        "Commitment paused by the controlled user",
                    ),
                    LifecycleStatus.ACTIVE: (
                        ObservationType.COMMITMENT_RESUMED,
                        ActivityEventType.COMMITMENT_RESUMED,
                        "Commitment resumed by the controlled user",
                    ),
                    LifecycleStatus.DISMISSED: (
                        ObservationType.COMMITMENT_DISMISSED,
                        ActivityEventType.COMMITMENT_DISMISSED,
                        "Commitment dismissed by the controlled user",
                    ),
                }
                observation_type, event_type, summary = event_map[lifecycle_target]
                details = {
                    "previous_status": commitment.lifecycle_status.value,
                    "target_status": lifecycle_target.value,
                }
                if lifecycle_target == LifecycleStatus.DISMISSED:
                    for approval in await repositories.approvals.list_pending(
                        actor.user_id
                    ):
                        if approval.get("commitment_id") != commitment_id:
                            continue
                        await repositories.approvals.resolve(
                            str(approval["approval_id"]),
                            int(approval["revision"]),
                            {
                                "status": "superseded",
                                "reason": "commitment_dismissed_by_user",
                                "actor": actor.user_id,
                            },
                        )
            await repositories.commitments.save(updated, commitment.revision)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=event_type,
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary=summary,
                    payload={
                        "commitment_id": commitment_id,
                        "commitment_revision": updated.revision,
                        **details,
                    },
                    created_at=now,
                )
            )
            observation = self._observation_factory.continuation(
                observation_type=observation_type,
                user_id=actor.user_id,
                producer_id=commitment_id,
                producer_version=str(updated.revision),
                safe_metadata={
                    "commitment_id": commitment_id,
                    "commitment_revision": updated.revision,
                    **details,
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            return CommandResult(
                CommandStatus.COMPLETED,
                {
                    "commitment_id": commitment_id,
                    "observation_id": observation.observation_id,
                },
                updated.revision,
                None,
            )

        try:
            result = await self._unit_of_work.run(_apply)
        except DomainError as error:
            return self._failure(commitment_id, type(error).__name__)
        observation_id = result.identifiers.get("observation_id")
        if result.status == CommandStatus.COMPLETED and observation_id:
            try:
                await self._observation_dispatcher.dispatch(observation_id)
            except Exception:  # noqa: BLE001 - periodic dispatch repairs the gap
                pass
        return result

    @staticmethod
    def _failure(commitment_id: str, error_code: str) -> CommandResult:
        return CommandResult(
            CommandStatus.TERMINAL_FAILURE,
            {"commitment_id": commitment_id},
            None,
            error_code,
        )
