from __future__ import annotations

from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class LoadObservationNode:
    def __init__(self, unit_of_work: UnitOfWork, workflow_version: str) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _validate_claim(self, state: ReconciliationStateV1) -> None:
        ...

    def _validate_processing_fence(self, state: ReconciliationStateV1) -> None:
        ...
