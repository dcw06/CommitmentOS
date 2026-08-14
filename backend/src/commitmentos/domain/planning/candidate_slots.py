from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.intervals import IntervalSet
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CandidateSlot,
    CommitmentDemand,
    PreservedWorkBlock,
    TimeInterval,
    UserPlanningPreferences,
)


class CandidateSlotGenerator:
    def __init__(self, grid_minutes: int) -> None:
        if grid_minutes <= 0 or 60 % grid_minutes != 0:
            raise ValueError("grid minutes must be a positive divisor of 60")
        self._grid_minutes = grid_minutes

    def free_capacity(
        self,
        planning_horizon: TimeInterval,
        busy_intervals: Sequence[CalendarBusyInterval],
        preserved_blocks: Sequence[PreservedWorkBlock],
        preferences: UserPlanningPreferences,
    ) -> tuple[TimeInterval, ...]:
        """Build the shared factual capacity pool before any allocation.

        Working windows are clipped to the horizon, then both external busy
        time and preserved CommitmentOS blocks are removed. `IntervalSet`
        normalizes overlaps, so an owned event represented in both inputs is
        subtracted once rather than double-counted.
        """
        zone = ZoneInfo(preferences.timezone)
        start_date = planning_horizon.start.astimezone(zone).date()
        end_date = planning_horizon.end.astimezone(zone).date()
        working: list[TimeInterval] = []
        day = start_date
        while day <= end_date:
            day_interval = TimeInterval(
                datetime.combine(day, preferences.working_day_start, tzinfo=zone),
                datetime.combine(day, preferences.working_day_end, tzinfo=zone),
            )
            working.extend(IntervalSet((day_interval,)).intersect(planning_horizon))
            day += timedelta(days=1)
        occupied = [item.interval for item in busy_intervals]
        occupied.extend(item.interval for item in preserved_blocks)
        return IntervalSet(working).subtract(occupied)

    def generate(
        self,
        free_intervals: Sequence[TimeInterval],
        demand: CommitmentDemand,
        preferences: UserPlanningPreferences,
        now: datetime,
    ) -> tuple[CandidateSlot, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("candidate generation requires a timezone-aware current time")
        maximum = min(
            preferences.maximum_block_minutes,
            demand.remaining_minutes,
        )
        minimum = preferences.minimum_block_minutes
        if maximum < minimum:
            return ()
        durations = range(
            self._align_duration(minimum),
            maximum + 1,
            self._grid_minutes,
        )
        candidates: dict[tuple[datetime, datetime], CandidateSlot] = {}
        deadline = demand.deadline.astimezone(timezone.utc)
        current = now.astimezone(timezone.utc)
        for free in free_intervals:
            start = self._ceil_to_grid(max(free.start.astimezone(timezone.utc), current))
            free_end = min(free.end.astimezone(timezone.utc), deadline)
            while start < free_end:
                for duration in durations:
                    end = start + timedelta(minutes=duration)
                    if end > free_end:
                        break
                    interval = TimeInterval(start, end)
                    candidates[(start, end)] = CandidateSlot(
                        interval=interval,
                        score=0,
                        score_components={},
                    )
                start += timedelta(minutes=self._grid_minutes)
        return tuple(
            candidates[key]
            for key in sorted(candidates, key=lambda value: (value[0], value[1]))
        )

    def split(
        self,
        interval: TimeInterval,
        minimum_minutes: int,
        maximum_minutes: int,
    ) -> tuple[TimeInterval, ...]:
        if minimum_minutes <= 0 or maximum_minutes < minimum_minutes:
            raise ValueError("invalid split bounds")
        total = interval.duration_minutes()
        if total < minimum_minutes:
            return ()
        block_count = max(1, (total + maximum_minutes - 1) // maximum_minutes)
        while block_count > 1 and total // block_count < minimum_minutes:
            block_count -= 1
        base_units = total // self._grid_minutes
        if base_units * self._grid_minutes != total:
            raise ValueError("interval duration must align to the configured grid")
        units_per_block, extra = divmod(base_units, block_count)
        durations = [
            (units_per_block + (1 if index < extra else 0)) * self._grid_minutes
            for index in range(block_count)
        ]
        if any(value < minimum_minutes or value > maximum_minutes for value in durations):
            return ()
        result: list[TimeInterval] = []
        cursor = interval.start
        for duration in durations:
            end = cursor.astimezone(timezone.utc) + timedelta(minutes=duration)
            localized_end = end.astimezone(interval.start.tzinfo)
            result.append(TimeInterval(cursor, localized_end))
            cursor = localized_end
        return tuple(result)

    def _align_duration(self, minutes: int) -> int:
        remainder = minutes % self._grid_minutes
        return minutes if remainder == 0 else minutes + self._grid_minutes - remainder

    def _ceil_to_grid(self, value: datetime) -> datetime:
        utc_value = value.astimezone(timezone.utc)
        seconds = int(utc_value.timestamp())
        grid_seconds = self._grid_minutes * 60
        aligned = ((seconds + grid_seconds - 1) // grid_seconds) * grid_seconds
        return datetime.fromtimestamp(aligned, tz=timezone.utc)
