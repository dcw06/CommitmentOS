from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PubSubPublishResult:
    message_id: str


class PubSubPublisher:
    def __init__(self, client: Any, project_id: str) -> None:
        ...

    async def publish(
        self,
        topic_name: str,
        payload: bytes,
        attributes: Mapping[str, str],
    ) -> PubSubPublishResult:
        ...

    def topic_path(self, topic_name: str) -> str:
        ...
