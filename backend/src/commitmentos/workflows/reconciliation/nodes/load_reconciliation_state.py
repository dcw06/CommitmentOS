from __future__ import annotations

from datetime import timedelta

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.workflows.reconciliation.state import (
    ExecutionControlSnapshot,
    PortfolioSnapshot,
    ReconciliationStateV1,
)


class LoadReconciliationStateNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        clock: Clock,
        planning_horizon: timedelta,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    async def _load_portfolio_snapshot(
        self,
        state: ReconciliationStateV1,
    ) -> PortfolioSnapshot:
        ...

    async def _load_control_snapshot(
        self,
        state: ReconciliationStateV1,
    ) -> ExecutionControlSnapshot:
        ...
