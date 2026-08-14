from __future__ import annotations

from typing import Protocol

from commitmentos.application.dto import ReconciliationOutcome, ReconciliationRequest


class ReconciliationWorkflow(Protocol):
    async def execute(self, request: ReconciliationRequest) -> ReconciliationOutcome:
        ...
