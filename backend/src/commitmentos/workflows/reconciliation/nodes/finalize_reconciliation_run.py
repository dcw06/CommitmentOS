from __future__ import annotations

from typing import Any, Mapping

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.contracts.observations import ReconciliationStatus
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class FinalizeReconciliationRunNode:
    def __init__(self, unit_of_work: UnitOfWork, clock: Clock) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _safe_run_record(self, state: ReconciliationStateV1) -> Mapping[str, Any]:
        ...

    def _terminal_observation_status(
        self,
        state: ReconciliationStateV1,
    ) -> ReconciliationStatus:
        ...
