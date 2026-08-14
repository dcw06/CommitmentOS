from __future__ import annotations

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.domain.evidence.models import EvidenceFactory
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class UpsertEvidenceNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        evidence_factory: EvidenceFactory,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    async def _upsert_commitment_facts(self, state: ReconciliationStateV1) -> tuple[str, ...]:
        ...
