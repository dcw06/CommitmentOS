from __future__ import annotations

from commitmentos.domain.policy.evaluator import PolicyEvaluator
from commitmentos.domain.policy.models import PolicyContext
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ApplyPolicyNode:
    def __init__(self, policy_evaluator: PolicyEvaluator) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _contexts(self, state: ReconciliationStateV1) -> tuple[PolicyContext, ...]:
        ...
