from __future__ import annotations

from dataclasses import dataclass

from commitmentos.application.dto import AuthenticatedActor, CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType


@dataclass(frozen=True, slots=True)
class PlanUndoRequest:
    planner_run_id: str
    idempotency_key: str


class RequestPlanUndo:
    """Record undo as new reconciliation input, never as state reversal."""

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
        request: PlanUndoRequest,
        trace_id: str,
    ) -> CommandResult:
        if not request.planner_run_id:
            raise ValueError("planner run ID is required")
        if not request.idempotency_key or len(request.idempotency_key) > 128:
            raise ValueError("idempotency key must contain 1 to 128 characters")
        now = self._clock.now()
        producer_id = (
            f"plan-undo:{actor.user_id}:{request.planner_run_id}:"
            f"{request.idempotency_key}"
        )
        observation = self._observation_factory.continuation(
            observation_type=ObservationType.PLAN_UNDO_REQUESTED,
            user_id=actor.user_id,
            producer_id=producer_id,
            producer_version="1",
            safe_metadata={
                "planner_run_id": request.planner_run_id,
                "requested_by": actor.user_id,
                "mode": "replan_from_current_facts",
            },
            observed_at=now,
            trace_id=trace_id,
        )

        async def _record(repositories: RepositorySet) -> CommandResult:
            plan = await repositories.planner_runs.get(request.planner_run_id)
            if plan is None:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"planner_run_id": request.planner_run_id},
                    None,
                    "planner_run_not_found",
                )
            if plan.user_id != actor.user_id:
                return CommandResult(
                    CommandStatus.TERMINAL_FAILURE,
                    {"planner_run_id": request.planner_run_id},
                    None,
                    "planner_run_forbidden",
                )
            created = await repositories.observations.create(observation)
            if not created:
                return CommandResult(
                    CommandStatus.NO_OP,
                    {
                        "planner_run_id": request.planner_run_id,
                        "observation_id": observation.observation_id,
                    },
                    None,
                    "undo_already_requested",
                )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=ActivityEventType.PLAN_UNDO_REQUESTED,
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary="Plan undo requested; reconciliation will use current facts",
                    payload={
                        "planner_run_id": request.planner_run_id,
                        "observation_id": observation.observation_id,
                        "mode": "replan_from_current_facts",
                    },
                    created_at=now,
                )
            )
            return CommandResult(
                CommandStatus.COMPLETED,
                {
                    "planner_run_id": request.planner_run_id,
                    "observation_id": observation.observation_id,
                },
                None,
                None,
            )

        result = await self._unit_of_work.run(_record)
        if result.status == CommandStatus.COMPLETED:
            try:
                await self._observation_dispatcher.dispatch(observation.observation_id)
            except Exception:  # noqa: BLE001 - durable observation repairs the gap
                pass
        return result
