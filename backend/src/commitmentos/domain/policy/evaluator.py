from __future__ import annotations

from commitmentos.domain.policy.models import PolicyContext, PolicyDecision, PolicyProfile


class PolicyEvaluator:
    def __init__(self, profile: PolicyProfile) -> None:
        ...

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        ...

    def is_extensive_change(self, context: PolicyContext) -> bool:
        ...
