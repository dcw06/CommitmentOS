from __future__ import annotations

from commitmentos.application.dto import (
    AuthenticatedActor,
    CommandResult,
    CommandStatus,
    ControlChangeRequest,
)
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.actions.models import ExecutionStatus
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.controls.models import ControlMode, SystemControls
from commitmentos.domain.shared.errors import DomainError

CONTROL_NAMES = {"monitoring", "automatic_actions"}
CONTROL_ACTIVITY_SCAN_LIMIT = 250


class ChangeSystemControl:
    """Change a durable execution control and continue through an observation.

    One transaction verifies the expected epoch, applies the change (which
    increments the monotonic control epoch), appends activity, and creates the
    `system_control_changed` observation. Resume work is driven by that
    observation, never by blindly releasing held records.
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
        request: ControlChangeRequest,
        trace_id: str,
    ) -> CommandResult:
        if request.control_name not in CONTROL_NAMES:
            raise ValueError(f"unknown control: {request.control_name}")
        target_mode = ControlMode(request.target_mode)
        now = self._clock.now()

        async def _change(repositories: RepositorySet) -> CommandResult:
            controls = await repositories.system_controls.get(actor.user_id)
            if controls.control_epoch != request.expected_control_epoch:
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"user_id": actor.user_id},
                    revision=controls.control_epoch,
                    error_code="control_epoch_conflict",
                )
            updated = self._apply_change(controls, request, actor.user_id, target_mode, now)
            held_actions = tuple(
                await repositories.outbox.list_held(
                    actor.user_id, CONTROL_ACTIVITY_SCAN_LIMIT
                )
            )
            in_flight_actions = tuple(
                await repositories.outbox.list_for_user_statuses(
                    actor.user_id,
                    (
                        ExecutionStatus.CLAIMED.value,
                        ExecutionStatus.ACTION_IN_FLIGHT.value,
                        ExecutionStatus.EXTERNAL_VERIFICATION_PENDING.value,
                    ),
                    CONTROL_ACTIVITY_SCAN_LIMIT,
                )
            )
            await repositories.system_controls.save(updated, controls.control_epoch)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=ActivityEventType.CONTROL_CHANGED,
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary=f"{request.control_name} set to {target_mode.value}",
                    payload={
                        "control_name": request.control_name,
                        "target_mode": target_mode.value,
                        "reason": request.reason,
                        "previous_epoch": controls.control_epoch,
                        "control_epoch": updated.control_epoch,
                        "held_action_count": len(held_actions),
                        "in_flight_action_count": len(in_flight_actions),
                    },
                    created_at=now,
                )
            )
            observation = self._observation_factory.continuation(
                observation_type=ObservationType.SYSTEM_CONTROL_CHANGED,
                user_id=actor.user_id,
                producer_id=actor.user_id,
                producer_version=str(updated.control_epoch),
                safe_metadata={
                    "control_name": request.control_name,
                    "target_mode": target_mode.value,
                    "control_epoch": updated.control_epoch,
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            return CommandResult(
                status=CommandStatus.COMPLETED,
                identifiers={
                    "user_id": actor.user_id,
                    "observation_id": observation.observation_id,
                },
                revision=updated.control_epoch,
                error_code=None,
            )

        try:
            result = await self._unit_of_work.run(_change)
        except DomainError as error:
            return CommandResult(
                status=CommandStatus.TERMINAL_FAILURE,
                identifiers={"user_id": actor.user_id},
                revision=None,
                error_code=type(error).__name__,
            )
        if result.status == CommandStatus.COMPLETED:
            await self._redispatch_released_work(actor.user_id, request, target_mode, result)
        return result

    def _apply_change(
        self,
        controls: SystemControls,
        request: ControlChangeRequest,
        actor_id: str,
        target_mode: ControlMode,
        now: object,
    ) -> SystemControls:
        if request.control_name == "monitoring":
            return controls.set_observation_mode(target_mode, actor_id, request.reason, now)  # type: ignore[arg-type]
        return controls.set_automatic_action_mode(target_mode, actor_id, request.reason, now)  # type: ignore[arg-type]

    async def _redispatch_released_work(
        self,
        user_id: str,
        request: ControlChangeRequest,
        target_mode: ControlMode,
        result: CommandResult,
    ) -> None:
        observation_id = result.identifiers.get("observation_id")
        if observation_id:
            try:
                await self._observation_dispatcher.dispatch(observation_id)
            except Exception:  # noqa: BLE001 - dispatch is repairable
                pass
        if request.control_name == "monitoring" and target_mode == ControlMode.ENABLED:
            # Resume: redispatch held observations with a fresh dispatch
            # generation; each resumed run reloads current durable facts.
            await self._observation_dispatcher.release_held(user_id, limit=50)
