from __future__ import annotations

from datetime import datetime


class SystemClock:
    def now(self) -> datetime:
        ...


class SecureIdGenerator:
    def new_id(self, prefix: str) -> str:
        ...

    def new_token(self, byte_length: int) -> str:
        ...
