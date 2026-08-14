from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ResetPreview:
    user_id: str
    firestore_document_count: int
    owned_calendar_event_count: int
    active_watch_count: int


class ControlledAccountResetter:
    def __init__(
        self,
        firestore_client: Any,
        calendar_client: Any,
        user_id: str,
        ownership_key: str,
    ) -> None:
        ...

    async def preview(self) -> ResetPreview:
        ...

    async def reset(self, expected_preview: ResetPreview, confirmation: str) -> None:
        ...

    def confirmation_phrase(self, preview: ResetPreview) -> str:
        ...


def main() -> int:
    ...
