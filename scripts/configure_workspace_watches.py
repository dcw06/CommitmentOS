from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from commitmentos.application.ports.calendar_reader import CalendarReader, CalendarWatch
from commitmentos.application.ports.gmail_reader import GmailReader, GmailWatch


@dataclass(frozen=True, slots=True)
class WatchConfigurationResult:
    gmail_watch: GmailWatch
    calendar_watch: CalendarWatch
    configured_at: datetime


class WorkspaceWatchConfigurator:
    def __init__(
        self,
        gmail_reader: GmailReader,
        calendar_reader: CalendarReader,
        user_id: str,
        calendar_id: str,
    ) -> None:
        ...

    async def configure(
        self,
        gmail_topic: str,
        gmail_label_ids: Sequence[str],
        calendar_callback_url: str,
    ) -> WatchConfigurationResult:
        ...

    async def verify(self, result: WatchConfigurationResult) -> None:
        ...


def main() -> int:
    ...
