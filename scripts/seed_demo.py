from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


class DemoSeeder:
    def __init__(self, source_directory: Path, target_directory: Path) -> None:
        ...

    def validate(self) -> None:
        ...

    def seed(self) -> tuple[Path, ...]:
        ...

    def _load_json(self, path: Path) -> Mapping[str, Any] | Sequence[Mapping[str, Any]]:
        ...


def main() -> int:
    ...
