from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


class DemoReadModel:
    def __init__(self, data_directory: Path) -> None:
        ...

    async def get_today(self) -> Mapping[str, Any]:
        ...

    async def list_commitments(self) -> Sequence[Mapping[str, Any]]:
        ...

    async def list_activity(self) -> Sequence[Mapping[str, Any]]:
        ...

    def _read_json(self, filename: str) -> Any:
        ...
