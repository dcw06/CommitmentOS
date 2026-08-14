from __future__ import annotations

from commitmentos.contracts.model_output import ModelOutputValidator
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ValidateInterpretationNode:
    def __init__(self, validator: ModelOutputValidator) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _allowed_target_ids(self, state: ReconciliationStateV1) -> set[str]:
        ...
