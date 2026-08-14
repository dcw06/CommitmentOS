from __future__ import annotations

from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class CalculatePortfolioFeasibilityNode:
    def __init__(self, portfolio_planner: PortfolioPlanner) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _validate_input_revisions(self, state: ReconciliationStateV1) -> None:
        ...
