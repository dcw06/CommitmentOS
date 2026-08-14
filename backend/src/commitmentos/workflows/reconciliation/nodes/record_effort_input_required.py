from __future__ import annotations

from typing import Any, Mapping

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class RecordEffortInputRequiredNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _approval_payload(self, state: ReconciliationStateV1) -> Mapping[str, Any]:
        ...
