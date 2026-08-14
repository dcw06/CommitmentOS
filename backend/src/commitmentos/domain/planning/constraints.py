from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.intervals import IntervalSet
from commitmentos.domain.planning.models import (
    CommitmentDemand,
    TimeInterval,
    UserPlanningPreferences,
)


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    code: str
    message: str


class ConstraintEvaluator:
    def evaluate(
        self,
        interval: TimeInterval,
        demand: CommitmentDemand,
        preferences: UserPlanningPreferences,
        occupied: Sequence[TimeInterval],
        now: datetime,
        daily_focus: Sequence[TimeInterval] = (),
    ) -> tuple[ConstraintViolation, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("constraint evaluation requires a timezone-aware current time")
        violations: list[ConstraintViolation] = []
        duration = interval.duration_minutes()
        zone = ZoneInfo(preferences.timezone)
        local_start = interval.start.astimezone(zone)
        local_end = interval.end.astimezone(zone)

        if interval.start.astimezone(timezone.utc) < now.astimezone(timezone.utc):
            violations.append(ConstraintViolation("in_past", "work cannot start in the past"))
        if interval.end.astimezone(timezone.utc) > demand.deadline.astimezone(timezone.utc):
            violations.append(
                ConstraintViolation("after_deadline", "work must finish by the commitment deadline")
            )
        if local_start.date() != local_end.date():
            violations.append(
                ConstraintViolation("outside_working_hours", "work blocks cannot cross working days")
            )
        elif (
            local_start.timetz().replace(tzinfo=None) < preferences.working_day_start
            or local_end.timetz().replace(tzinfo=None) > preferences.working_day_end
        ):
            violations.append(
                ConstraintViolation("outside_working_hours", "work must stay inside working hours")
            )
        if duration < preferences.minimum_block_minutes:
            violations.append(
                ConstraintViolation("below_minimum_block", "work block is shorter than the minimum")
            )
        if duration > preferences.maximum_block_minutes:
            violations.append(
                ConstraintViolation("above_maximum_block", "work block is longer than the maximum")
            )
        if any(interval.overlaps(existing) for existing in occupied):
            violations.append(ConstraintViolation("overlap", "work block overlaps occupied time"))

        day_start = datetime.combine(local_start.date(), time.min, tzinfo=zone)
        day_end = datetime.combine(local_start.date() + timedelta(days=1), time.min, tzinfo=zone)
        day_bounds = TimeInterval(day_start, day_end)
        already_reserved = sum(
            part.duration_minutes()
            for part in IntervalSet(daily_focus).intersect(day_bounds)
        )
        if already_reserved + duration > preferences.daily_focus_limit_minutes:
            violations.append(
                ConstraintViolation("daily_focus_limit", "daily focus-work limit would be exceeded")
            )
        return tuple(violations)

    def is_valid(
        self,
        interval: TimeInterval,
        demand: CommitmentDemand,
        preferences: UserPlanningPreferences,
        occupied: Sequence[TimeInterval],
        now: datetime,
        daily_focus: Sequence[TimeInterval] = (),
    ) -> bool:
        return not self.evaluate(
            interval,
            demand,
            preferences,
            occupied,
            now,
            daily_focus,
        )
