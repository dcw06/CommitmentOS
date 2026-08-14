from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class GoldenPathResult:
    started_at: datetime
    completed_at: datetime
    passed: bool
    checkpoints: tuple[Mapping[str, Any], ...]


class GoldenPathRunner:
    def __init__(self, http_client: Any, service_base_url: str) -> None:
        ...

    async def run(self) -> GoldenPathResult:
        ...

    async def wait_for_commitment(self, external_reference: str) -> Mapping[str, Any]:
        ...

    async def wait_for_calendar_action(self, commitment_id: str) -> Mapping[str, Any]:
        ...

    async def verify_activity_chain(self, commitment_id: str) -> tuple[Mapping[str, Any], ...]:
        ...


def main() -> int:
    ...
