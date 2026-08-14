from __future__ import annotations

from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.application.services.projection_guard import ProjectionGuard
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ValidateProjectionsNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        projection_guard: ProjectionGuard,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    async def _rebuild_stale_projections(self, state: ReconciliationStateV1) -> tuple[str, ...]:
        ...
