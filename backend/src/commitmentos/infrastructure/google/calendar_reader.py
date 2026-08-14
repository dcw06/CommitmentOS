from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, time, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build as build_google_api  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from commitmentos.application.ports.calendar_reader import (
    CalendarEventRecord,
    CalendarSyncPage,
    CalendarWatch,
)
from commitmentos.application.ports.gmail_reader import (
    SourceAuthorizationError,
    SourceCursorInvalidError,
)
from commitmentos.domain.planning.intervals import IntervalSet
from commitmentos.domain.planning.models import CalendarBusyInterval, TimeInterval
from commitmentos.domain.shared.types import CanonicalEncoder
from commitmentos.infrastructure.google.credentials import ControlledCredentialsProvider


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class GoogleCalendarReader:
    """Read-only Calendar access for the executor and (Phase 4) synchronization."""

    def __init__(
        self,
        credentials_provider: ControlledCredentialsProvider,
        service_factory: Callable[[], Any] | None = None,
        sync_page_size: int = 250,
    ) -> None:
        if not 1 <= sync_page_size <= 250:
            raise ValueError("Calendar sync page size must be between 1 and 250")
        self._credentials_provider = credentials_provider
        self._service_factory = service_factory
        self._sync_page_size = sync_page_size

    def _service(self) -> Any:
        if self._service_factory is not None:
            return self._service_factory()
        return build_google_api(
            "calendar",
            "v3",
            credentials=self._credentials_provider.credentials(),
            cache_discovery=False,
        )

    async def list_busy_intervals(
        self,
        calendar_id: str,
        planning_horizon: TimeInterval,
        timezone_name: str,
    ) -> tuple[CalendarBusyInterval, ...]:
        """Read expanded Calendar instances for the bounded planning horizon.

        `singleEvents=True` asks Calendar to expand recurring events. All-day
        dates are interpreted in the controlled user's timezone and retain
        Google's exclusive end-date semantics. Transparent and canceled
        events do not consume capacity.
        """

        def _blocking() -> tuple[CalendarBusyInterval, ...]:
            service = self._service()
            page_token: str | None = None
            busy: list[CalendarBusyInterval] = []
            while True:
                response = (
                    service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=planning_horizon.start.astimezone(timezone.utc).isoformat(),
                        timeMax=planning_horizon.end.astimezone(timezone.utc).isoformat(),
                        timeZone=timezone_name,
                        singleEvents=True,
                        orderBy="startTime",
                        showDeleted=False,
                        maxResults=2500,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for event in response.get("items", []):
                    normalized = calendar_event_to_busy_interval(
                        calendar_id,
                        event,
                        planning_horizon,
                        timezone_name,
                    )
                    if normalized is not None:
                        busy.append(normalized)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return tuple(
                sorted(
                    busy,
                    key=lambda item: (
                        item.interval.start.astimezone(timezone.utc),
                        item.interval.end.astimezone(timezone.utc),
                        item.calendar_event_id,
                    ),
                )
            )

        return await asyncio.to_thread(_blocking)

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventRecord | None:
        def _blocking() -> CalendarEventRecord | None:
            try:
                event = (
                    self._service()
                    .events()
                    .get(calendarId=calendar_id, eventId=event_id)
                    .execute()
                )
            except HttpError as error:
                status = error.resp.status if error.resp is not None else 0
                if status in (404, 410):
                    return None
                raise
            return CalendarEventRecord(
                calendar_id=calendar_id,
                event_id=event.get("id", ""),
                etag=event.get("etag"),
                status=event.get("status", "unknown"),
                payload=dict(event),
                payload_hash=_payload_hash(event),
            )

        return await asyncio.to_thread(_blocking)

    async def create_events_watch(
        self,
        calendar_id: str,
        callback_url: str,
        channel_id: str,
        channel_token: str,
        expiration: datetime,
    ) -> CalendarWatch:
        def _blocking() -> CalendarWatch:
            response = (
                self._service()
                .events()
                .watch(
                    calendarId=calendar_id,
                    body={
                        "id": channel_id,
                        "type": "web_hook",
                        "address": callback_url,
                        "token": channel_token,
                        "expiration": str(
                            int(expiration.astimezone(timezone.utc).timestamp() * 1000)
                        ),
                    },
                )
                .execute()
            )
            expiration_value = response.get("expiration")
            return CalendarWatch(
                channel_id=str(response.get("id") or channel_id),
                resource_id=str(response["resourceId"]),
                resource_uri=str(response.get("resourceUri") or ""),
                expiration=(
                    datetime.fromtimestamp(int(expiration_value) / 1000, tz=timezone.utc)
                    if expiration_value is not None
                    else None
                ),
                token_hash=hashlib.sha256(channel_token.encode()).hexdigest(),
            )

        try:
            return await asyncio.to_thread(_blocking)
        except RefreshError as error:
            self._credentials_provider.invalidate()
            raise SourceAuthorizationError("calendar credential refresh failed") from error

    async def stop_watch(self, channel_id: str, resource_id: str) -> None:
        def _blocking() -> None:
            self._service().channels().stop(
                body={"id": channel_id, "resourceId": resource_id}
            ).execute()

        try:
            await asyncio.to_thread(_blocking)
        except RefreshError as error:
            self._credentials_provider.invalidate()
            raise SourceAuthorizationError("calendar credential refresh failed") from error

    async def sync_events(
        self,
        calendar_id: str,
        sync_token: str | None,
        page_token: str | None,
        time_min: datetime | None,
        time_max: datetime | None,
    ) -> CalendarSyncPage:
        def _blocking() -> CalendarSyncPage:
            request_kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "showDeleted": True,
                "singleEvents": True,
                "maxResults": self._sync_page_size,
            }
            if page_token is not None:
                request_kwargs["pageToken"] = page_token
            if sync_token is not None:
                # Google forbids time bounds and orderBy with syncToken. The
                # token carries the exact frozen collection semantics.
                request_kwargs["syncToken"] = sync_token
            else:
                # Do not order synchronization pages. `orderBy` is outside
                # Calendar's sync-token query family and prevents the final
                # page from yielding the token required for incrementals.
                if time_min is not None:
                    request_kwargs["timeMin"] = time_min.astimezone(timezone.utc).isoformat()
                if time_max is not None:
                    request_kwargs["timeMax"] = time_max.astimezone(timezone.utc).isoformat()
            try:
                response = self._service().events().list(**request_kwargs).execute()
            except RefreshError as error:
                self._credentials_provider.invalidate()
                raise SourceAuthorizationError("calendar credential refresh failed") from error
            except HttpError as error:
                status = error.resp.status if error.resp is not None else 0
                if status == 410:
                    raise SourceCursorInvalidError(
                        "calendar sync token is no longer valid"
                    ) from error
                raise
            events = tuple(
                CalendarEventRecord(
                    calendar_id=calendar_id,
                    event_id=str(event.get("id") or ""),
                    etag=(str(event["etag"]) if event.get("etag") is not None else None),
                    status=str(event.get("status") or "unknown"),
                    payload=dict(event),
                    payload_hash=_payload_hash(event),
                )
                for event in response.get("items", [])
                if event.get("id")
            )
            return CalendarSyncPage(
                events=events,
                next_page_token=response.get("nextPageToken"),
                next_sync_token=response.get("nextSyncToken"),
            )

        return await asyncio.to_thread(_blocking)


def calendar_event_to_busy_interval(
    calendar_id: str,
    event: Mapping[str, Any],
    planning_horizon: TimeInterval,
    timezone_name: str,
) -> CalendarBusyInterval | None:
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return None
    start = _event_boundary(event.get("start"), timezone_name)
    end = _event_boundary(event.get("end"), timezone_name)
    if start is None or end is None:
        return None
    try:
        event_interval = TimeInterval(start, end)
    except ValueError:
        return None
    clipped = IntervalSet((event_interval,)).intersect(planning_horizon)
    if not clipped:
        return None
    properties = event.get("extendedProperties", {}).get("private", {})
    event_id = str(event.get("id", ""))
    payload_hash = _payload_hash(event)
    etag = str(event.get("etag") or payload_hash)
    source_revision = int(hashlib.sha256(etag.encode()).hexdigest()[:15], 16)
    snapshot_id = CanonicalEncoder.hash(
        [
            "live-calendar-busy:v1",
            calendar_id,
            event_id,
            etag,
            clipped[0].start,
            clipped[0].end,
        ]
    )
    is_app_owned = (
        properties.get("managed_by") == "commitmentos"
        and bool(properties.get("work_block_id"))
    )
    return CalendarBusyInterval(
        snapshot_id=snapshot_id,
        calendar_event_id=event_id,
        interval=clipped[0],
        is_app_owned=is_app_owned,
        work_block_id=(str(properties["work_block_id"]) if is_app_owned else None),
        source_revision=source_revision,
    )


def _event_boundary(
    value: object,
    default_timezone: str,
) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    date_time_value = value.get("dateTime")
    if date_time_value:
        parsed = datetime.fromisoformat(str(date_time_value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(
                tzinfo=ZoneInfo(str(value.get("timeZone") or default_timezone))
            )
        return parsed
    date_value = value.get("date")
    if date_value:
        # Calendar all-day `end.date` is already exclusive; both boundaries
        # therefore normalize to local midnight without adding a day here.
        parsed_date = date.fromisoformat(str(date_value))
        return datetime.combine(parsed_date, time.min, tzinfo=ZoneInfo(default_timezone))
    return None
