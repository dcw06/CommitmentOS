from __future__ import annotations

from commitmentos.domain.planning.diff import PlanDiffer
from commitmentos.domain.planning.repair import StablePlanRepairer
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ProduceStablePlanNode:
    def __init__(
        self,
        repairer: StablePlanRepairer,
        plan_differ: PlanDiffer,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _validate_stability(self, state: ReconciliationStateV1) -> None:
        ...
