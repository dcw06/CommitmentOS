from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class DemoReadModel(Protocol):
    async def get_today(self) -> Mapping[str, Any]:
        ...

    async def list_commitments(self) -> Sequence[Mapping[str, Any]]:
        ...

    async def list_activity(self) -> Sequence[Mapping[str, Any]]:
        ...
