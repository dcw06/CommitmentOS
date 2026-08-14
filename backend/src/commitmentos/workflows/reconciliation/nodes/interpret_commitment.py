from __future__ import annotations

from commitmentos.application.ports.model_interpreter import ModelInterpreter
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.domain.commitments.models import Commitment
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class InterpretCommitmentNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        model_interpreter: ModelInterpreter,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    async def _load_identity_candidates(
        self,
        state: ReconciliationStateV1,
    ) -> tuple[Commitment, ...]:
        ...
