from __future__ import annotations

from commitmentos.application.ports.gmail_reader import GmailReader
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class LoadSourceContextNode:
    def __init__(self, gmail_reader: GmailReader, maximum_source_characters: int) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _truncate(self, source_text: str) -> str:
        ...
