from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from commitmentos.domain.planning.models import CalendarBusyInterval, TimeInterval


@dataclass(frozen=True, slots=True)
class CalendarEventRecord:
    calendar_id: str
    event_id: str
    etag: str | None
    status: str
    payload: Mapping[str, Any]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class CalendarSyncPage:
    events: tuple[CalendarEventRecord, ...]
    next_page_token: str | None
    next_sync_token: str | None


@dataclass(frozen=True, slots=True)
class CalendarWatch:
    channel_id: str
    resource_id: str
    resource_uri: str
    expiration: datetime | None
    token_hash: str


class CalendarReader(Protocol):
    async def list_busy_intervals(
        self,
        calendar_id: str,
        planning_horizon: TimeInterval,
        timezone_name: str,
    ) -> tuple[CalendarBusyInterval, ...]:
        ...

    async def create_events_watch(
        self,
        calendar_id: str,
        callback_url: str,
        channel_id: str,
        channel_token: str,
        expiration: datetime,
    ) -> CalendarWatch:
        ...

    async def stop_watch(self, channel_id: str, resource_id: str) -> None:
        ...

    async def sync_events(
        self,
        calendar_id: str,
        sync_token: str | None,
        page_token: str | None,
        time_min: datetime | None,
        time_max: datetime | None,
    ) -> CalendarSyncPage:
        ...

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventRecord | None:
        ...
