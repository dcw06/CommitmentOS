from __future__ import annotations

from typing import Protocol


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str:
        ...

    def new_token(self, byte_length: int) -> str:
        ...
