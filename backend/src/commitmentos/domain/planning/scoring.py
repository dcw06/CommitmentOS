from __future__ import annotations

from datetime import timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.models import (
    CommitmentDemand,
    TimeInterval,
    UserPlanningPreferences,
)


class SlotScorer:
    def __init__(self, score_version: str) -> None:
        if not score_version:
            raise ValueError("score version is required")
        self._score_version = score_version

    @property
    def score_version(self) -> str:
        return self._score_version

    def score(
        self,
        interval: TimeInterval,
        demand: CommitmentDemand,
        preferences: UserPlanningPreferences,
    ) -> tuple[int, Mapping[str, int]]:
        zone = ZoneInfo(preferences.timezone)
        local_start = interval.start.astimezone(zone)
        local_end = interval.end.astimezone(zone)
        preferred = int(
            any(
                local_start.date() == local_end.date()
                and start <= local_start.timetz().replace(tzinfo=None)
                and local_end.timetz().replace(tzinfo=None) <= end
                for start, end in preferences.preferred_focus_windows
            )
        )
        deadline_buffer = max(
            int(
                (
                    demand.deadline.astimezone(timezone.utc)
                    - interval.end.astimezone(timezone.utc)
                ).total_seconds()
                // 60
            ),
            0,
        )
        components = {
            # Preferred placement is the strongest static preference. Daily
            # balance and back-to-back avoidance are portfolio-relative and
            # are applied by `PortfolioPlanner` before this score.
            "preferred_focus": preferred * 1_000_000,
            "deadline_buffer": min(deadline_buffer, 100_000),
            "fewer_fragments": interval.duration_minutes() * 100,
        }
        return sum(components.values()), components
