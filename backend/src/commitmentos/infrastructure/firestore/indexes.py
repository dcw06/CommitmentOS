from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FirestoreIndexSpec:
    collection_group: str
    fields: tuple[tuple[str, str], ...]


class FirestoreIndexRegistry:
    def required(self) -> tuple[FirestoreIndexSpec, ...]:
        ...

    def load_deployed(self, indexes_file: Path) -> tuple[FirestoreIndexSpec, ...]:
        ...

    def missing_from(self, deployed: tuple[FirestoreIndexSpec, ...]) -> tuple[FirestoreIndexSpec, ...]:
        ...
