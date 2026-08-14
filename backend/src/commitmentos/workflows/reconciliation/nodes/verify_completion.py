from __future__ import annotations

from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class VerifyCompletionNode:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    async def _verify_evidence(self, state: ReconciliationStateV1) -> str:
        ...

    async def _transition_commitment(self, state: ReconciliationStateV1, evidence_id: str) -> str:
        ...
