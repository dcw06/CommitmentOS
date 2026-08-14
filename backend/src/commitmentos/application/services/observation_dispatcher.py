from __future__ import annotations

from dataclasses import dataclass

from commitmentos.application.ports.task_dispatcher import TaskDispatcher, TaskDispatchResult
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.observations import ObservationV1, ReconciliationStatus
from commitmentos.contracts.tasks import ReconcileObservationTaskV1


@dataclass(frozen=True, slots=True)
class ObservationDispatchSummary:
    scanned: int
    queued: int
    held: int
    already_queued: int


class ObservationDispatcher:
    """Moves durable observations into the reconciliation Cloud Tasks queue.

    Normalized observations are never delivered through Pub/Sub; this
    dispatcher is the only path from a committed observation to a named
    reconciliation task. Task creation happens strictly after the status
    commit, and a failed enqueue stays repairable through `dispatch_pending`.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        task_dispatcher: TaskDispatcher,
        workflow_version: str,
        task_schema_version: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._task_dispatcher = task_dispatcher
        self._workflow_version = workflow_version
        self._task_schema_version = task_schema_version

    async def dispatch(self, observation_id: str) -> TaskDispatchResult | None:
        async def _mark(repositories: RepositorySet) -> ObservationV1 | None:
            observation = await repositories.observations.get(observation_id)
            if observation is None:
                return None
            if observation.reconciliation_status == ReconciliationStatus.QUEUED:
                # Already queued: recreate the (idempotent) named task for repair.
                return observation
            if observation.reconciliation_status != ReconciliationStatus.PENDING:
                return None
            controls = await repositories.system_controls.get(observation.user_id)
            if not controls.allows_observation_processing():
                await repositories.observations.set_reconciliation_status(
                    observation_id,
                    {ReconciliationStatus.PENDING},
                    ReconciliationStatus.HELD_BY_CONTROL,
                    observation.dispatch_generation,
                    controls.control_epoch,
                    None,
                    None,
                )
                return None
            return await repositories.observations.set_reconciliation_status(
                observation_id,
                {ReconciliationStatus.PENDING},
                ReconciliationStatus.QUEUED,
                observation.dispatch_generation,
                None,
                None,
                None,
            )

        observation = await self._unit_of_work.run(_mark)
        if observation is None:
            return None
        return await self._task_dispatcher.enqueue_reconciliation(self._task_for(observation))

    async def dispatch_pending(self, user_id: str, limit: int) -> ObservationDispatchSummary:
        async def _list(repositories: RepositorySet) -> list[ObservationV1]:
            return list(await repositories.observations.list_pending(user_id, limit))

        pending = await self._unit_of_work.read(_list)
        queued = 0
        held = 0
        for observation in pending:
            result = await self.dispatch(observation.observation_id)
            if result is not None:
                queued += 1
            else:
                held += 1
        return ObservationDispatchSummary(
            scanned=len(pending),
            queued=queued,
            held=held,
            already_queued=0,
        )

    async def release_held(self, user_id: str, limit: int) -> ObservationDispatchSummary:
        """Redispatch held observations after a monitoring resume.

        Each release increments the dispatch generation so the new named task
        cannot collide with the acknowledged pre-pause task name, and every
        resumed run reloads current durable state.
        """

        async def _list(repositories: RepositorySet) -> list[ObservationV1]:
            return list(await repositories.observations.list_held(user_id, limit))

        held_observations = await self._unit_of_work.read(_list)
        ordered = sorted(held_observations, key=lambda o: (o.observed_at, o.observation_id))
        queued = 0
        for held in ordered:

            async def _release(
                repositories: RepositorySet,
                observation_id: str = held.observation_id,
            ) -> ObservationV1 | None:
                observation = await repositories.observations.get(observation_id)
                if observation is None:
                    return None
                if observation.reconciliation_status != ReconciliationStatus.HELD_BY_CONTROL:
                    return None
                controls = await repositories.system_controls.get(observation.user_id)
                if not controls.allows_observation_processing():
                    return None
                return await repositories.observations.set_reconciliation_status(
                    observation_id,
                    {ReconciliationStatus.HELD_BY_CONTROL},
                    ReconciliationStatus.QUEUED,
                    observation.dispatch_generation + 1,
                    None,
                    None,
                    None,
                )

            released = await self._unit_of_work.run(_release)
            if released is not None:
                await self._task_dispatcher.enqueue_reconciliation(self._task_for(released))
                queued += 1
        return ObservationDispatchSummary(
            scanned=len(held_observations),
            queued=queued,
            held=len(held_observations) - queued,
            already_queued=0,
        )

    def _task_for(self, observation: ObservationV1) -> ReconcileObservationTaskV1:
        return ReconcileObservationTaskV1(
            schema_version=self._task_schema_version,
            observation_id=observation.observation_id,
            workflow_version=self._workflow_version,
            dispatch_generation=observation.dispatch_generation,
            trace_id=observation.trace_id,
        )
