from __future__ import annotations

from typing import Any

from commitmentos.application.ports.demo_read_model import DemoReadModel


class DemoRouter:
    def __init__(self, read_model: DemoReadModel) -> None:
        ...

    def build(self) -> Any:
        ...

    async def today(self) -> Any:
        ...

    async def commitments(self) -> Any:
        ...

    async def activity(self) -> Any:
        ...
