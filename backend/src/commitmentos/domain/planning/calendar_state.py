from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.models import CalendarBusyInterval, TimeInterval
from commitmentos.domain.shared.types import CanonicalEncoder


@dataclass(frozen=True, slots=True)
class CalendarEventSnapshot:
    calendar_snapshot_id: str
    user_id: str
    calendar_id: str
    calendar_event_id: str
    calendar_state_revision: int
    observed_event_etag: str | None
    observed_status: str
    observed_start: datetime | None
    observed_end: datetime | None
    observed_timezone: str | None
    observed_all_day: bool
    observed_transparency: str | None
    observed_recurring_event_id: str | None
    observed_original_start_time: datetime | None
    observed_managed_by: str | None
    observed_commitment_id: str | None
    observed_work_block_id: str | None
    observed_plan_revision: int | None
    observed_payload_hash: str
    source_observation_id: str
    observed_at: datetime
    is_tombstone: bool
    deleted_at: datetime | None

    def is_app_owned(self) -> bool:
        return self.observed_managed_by == "commitmentos" and self.observed_work_block_id is not None

    def contributes_busy_time(self) -> bool:
        if self.is_tombstone or self.observed_status == "cancelled":
            return False
        if self.observed_transparency == "transparent":
            return False
        return self.observed_start is not None and self.observed_end is not None

    def interval(self) -> TimeInterval | None:
        if self.observed_start is None or self.observed_end is None:
            return None
        return TimeInterval(start=self.observed_start, end=self.observed_end)


@dataclass(frozen=True, slots=True)
class CalendarStateSnapshot:
    calendar_id: str
    calendar_state_revision: int
    calendar_snapshot_hash: str
    events: tuple[CalendarEventSnapshot, ...]


class CalendarSnapshotReducer:
    def reduce_change(
        self,
        previous: CalendarEventSnapshot | None,
        event: Mapping[str, Any],
        source_observation_id: str,
        calendar_state_revision: int,
        observed_at: datetime,
    ) -> CalendarEventSnapshot:
        calendar_id = str(event.get("calendar_id") or (previous.calendar_id if previous else ""))
        event_id = str(
            event.get("id")
            or event.get("calendar_event_id")
            or (previous.calendar_event_id if previous else "")
        )
        user_id = str(event.get("user_id") or (previous.user_id if previous else ""))
        if not calendar_id or not event_id or not user_id:
            raise ValueError("calendar snapshot changes require user, calendar, and event ids")

        default_timezone = str(
            event.get("default_timezone")
            or (previous.observed_timezone if previous else None)
            or "UTC"
        )
        start, start_all_day, start_timezone = self._boundary(
            event.get("start"), default_timezone
        )
        end, end_all_day, end_timezone = self._boundary(event.get("end"), default_timezone)
        properties = event.get("extendedProperties")
        private: Mapping[str, Any] = {}
        if isinstance(properties, Mapping):
            candidate = properties.get("private")
            if isinstance(candidate, Mapping):
                private = candidate

        status = str(event.get("status") or "confirmed")
        tombstone = bool(event.get("is_tombstone")) or status == "cancelled"
        payload_hash = str(event.get("payload_hash") or "")
        if not payload_hash:
            payload_hash = CanonicalEncoder.hash(
                [
                    "calendar-event-payload:v1",
                    event_id,
                    event.get("etag"),
                    status,
                    start,
                    end,
                    json.dumps(dict(private), sort_keys=True, default=str),
                ]
            )

        # Cancelled events returned by incremental sync often contain only an
        # id. Preserve ownership/provenance so the deletion classifier can
        # still identify a user-deleted CommitmentOS work block.
        def _private_or_previous(key: str, previous_value: Any) -> Any:
            value = private.get(key)
            return value if value is not None else previous_value

        plan_revision_value = _private_or_previous(
            "plan_revision", previous.observed_plan_revision if previous else None
        )
        try:
            plan_revision = (
                int(plan_revision_value) if plan_revision_value is not None else None
            )
        except (TypeError, ValueError):
            plan_revision = previous.observed_plan_revision if previous else None
        observed_start = start if start is not None else (previous.observed_start if tombstone and previous else None)
        observed_end = end if end is not None else (previous.observed_end if tombstone and previous else None)
        return CalendarEventSnapshot(
            calendar_snapshot_id=CanonicalEncoder.hash(
                ["calendar-snapshot:v1", calendar_id, event_id]
            ),
            user_id=user_id,
            calendar_id=calendar_id,
            calendar_event_id=event_id,
            calendar_state_revision=calendar_state_revision,
            observed_event_etag=(
                str(event["etag"])
                if event.get("etag") is not None
                else (previous.observed_event_etag if previous else None)
            ),
            observed_status=status,
            observed_start=observed_start,
            observed_end=observed_end,
            observed_timezone=start_timezone or end_timezone or default_timezone,
            observed_all_day=start_all_day or end_all_day or bool(event.get("all_day")),
            observed_transparency=(
                str(event["transparency"])
                if event.get("transparency") is not None
                else (previous.observed_transparency if tombstone and previous else None)
            ),
            observed_recurring_event_id=(
                str(event["recurringEventId"])
                if event.get("recurringEventId") is not None
                else (previous.observed_recurring_event_id if tombstone and previous else None)
            ),
            observed_original_start_time=self._boundary(
                event.get("originalStartTime"), default_timezone
            )[0]
            or (previous.observed_original_start_time if tombstone and previous else None),
            observed_managed_by=_private_or_previous(
                "managed_by", previous.observed_managed_by if previous else None
            ),
            observed_commitment_id=_private_or_previous(
                "commitment_id", previous.observed_commitment_id if previous else None
            ),
            observed_work_block_id=_private_or_previous(
                "work_block_id", previous.observed_work_block_id if previous else None
            ),
            observed_plan_revision=plan_revision,
            observed_payload_hash=payload_hash,
            source_observation_id=source_observation_id,
            observed_at=observed_at,
            is_tombstone=tombstone,
            deleted_at=observed_at if tombstone else None,
        )

    def busy_intervals(
        self,
        snapshots: Sequence[CalendarEventSnapshot],
        planning_horizon: TimeInterval,
    ) -> tuple[CalendarBusyInterval, ...]:
        busy: list[CalendarBusyInterval] = []
        for snapshot in snapshots:
            if not snapshot.contributes_busy_time():
                continue
            interval = snapshot.interval()
            if interval is None:
                continue
            start = max(interval.start, planning_horizon.start)
            end = min(interval.end, planning_horizon.end)
            if end <= start:
                continue
            busy.append(
                CalendarBusyInterval(
                    snapshot_id=snapshot.calendar_snapshot_id,
                    calendar_event_id=snapshot.calendar_event_id,
                    interval=TimeInterval(start, end),
                    is_app_owned=snapshot.is_app_owned(),
                    work_block_id=(
                        snapshot.observed_work_block_id if snapshot.is_app_owned() else None
                    ),
                    source_revision=snapshot.calendar_state_revision,
                )
            )
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

    def snapshot_hash(self, snapshots: Sequence[CalendarEventSnapshot]) -> str:
        parts: list[str | int | datetime | None] = ["calendar-state-snapshot:v1"]
        for snapshot in sorted(
            snapshots,
            key=lambda value: (value.calendar_id, value.calendar_event_id),
        ):
            parts.extend(
                [
                snapshot.calendar_id,
                snapshot.calendar_event_id,
                snapshot.observed_event_etag,
                snapshot.observed_status,
                snapshot.observed_start,
                snapshot.observed_end,
                snapshot.observed_timezone,
                int(snapshot.observed_all_day),
                snapshot.observed_transparency,
                snapshot.observed_recurring_event_id,
                snapshot.observed_original_start_time,
                snapshot.observed_managed_by,
                snapshot.observed_commitment_id,
                snapshot.observed_work_block_id,
                snapshot.observed_plan_revision,
                snapshot.observed_payload_hash,
                int(snapshot.is_tombstone),
                ]
            )
        return CanonicalEncoder.hash(parts)

    @staticmethod
    def _boundary(
        value: object,
        default_timezone: str,
    ) -> tuple[datetime | None, bool, str | None]:
        if not isinstance(value, Mapping):
            return None, False, None
        timezone_name = str(value.get("timeZone") or default_timezone)
        date_time_value = value.get("dateTime")
        if date_time_value:
            parsed = datetime.fromisoformat(str(date_time_value).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            return parsed, False, timezone_name
        date_value = value.get("date")
        if date_value:
            parsed_date = date.fromisoformat(str(date_value))
            return (
                datetime.combine(parsed_date, time.min, tzinfo=ZoneInfo(timezone_name)),
                True,
                timezone_name,
            )
        return None, False, timezone_name
