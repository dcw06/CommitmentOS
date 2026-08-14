from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from commitmentos.domain.planning.models import TimeInterval


class IntervalSet:
    def __init__(self, intervals: Sequence[TimeInterval]) -> None:
        self._intervals = tuple(intervals)

    @staticmethod
    def _instant(value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    def normalized(self) -> tuple[TimeInterval, ...]:
        ordered = sorted(
            self._intervals,
            key=lambda interval: (
                self._instant(interval.start),
                self._instant(interval.end),
            ),
        )
        if not ordered:
            return ()
        merged: list[TimeInterval] = [ordered[0]]
        for interval in ordered[1:]:
            previous = merged[-1]
            if self._instant(interval.start) <= self._instant(previous.end):
                if self._instant(interval.end) > self._instant(previous.end):
                    merged[-1] = TimeInterval(previous.start, interval.end)
                continue
            merged.append(interval)
        return tuple(merged)

    def subtract(self, busy: Sequence[TimeInterval]) -> tuple[TimeInterval, ...]:
        cuts = IntervalSet(busy).normalized()
        remaining: list[TimeInterval] = []
        for available in self.normalized():
            fragments = [available]
            for cut in cuts:
                next_fragments: list[TimeInterval] = []
                for fragment in fragments:
                    if not fragment.overlaps(cut):
                        next_fragments.append(fragment)
                        continue
                    if self._instant(fragment.start) < self._instant(cut.start):
                        next_fragments.append(TimeInterval(fragment.start, cut.start))
                    if self._instant(cut.end) < self._instant(fragment.end):
                        next_fragments.append(TimeInterval(cut.end, fragment.end))
                fragments = next_fragments
                if not fragments:
                    break
            remaining.extend(fragments)
        return tuple(remaining)

    def intersect(self, bounds: TimeInterval) -> tuple[TimeInterval, ...]:
        intersections: list[TimeInterval] = []
        for interval in self.normalized():
            start = max(interval.start, bounds.start, key=self._instant)
            end = min(interval.end, bounds.end, key=self._instant)
            if self._instant(start) < self._instant(end):
                intersections.append(TimeInterval(start, end))
        return tuple(intersections)

    def contains(self, candidate: TimeInterval) -> bool:
        return any(interval.contains(candidate) for interval in self.normalized())

    def total_minutes(self) -> int:
        return sum(interval.duration_minutes() for interval in self.normalized())


class IntervalFactory:
    def from_bounds(self, start: datetime, end: datetime) -> TimeInterval:
        return TimeInterval(start=start, end=end)
