from __future__ import annotations

from datetime import time
from typing import Any, Mapping

from commitmentos.domain.planning.models import UserPlanningPreferences


def default_user_planning_preferences(timezone: str) -> UserPlanningPreferences:
    """Frozen P0 defaults from `golden_scenario_rev_1`."""
    return UserPlanningPreferences(
        timezone=timezone,
        working_day_start=time(9, 0),
        working_day_end=time(17, 30),
        minimum_block_minutes=30,
        maximum_block_minutes=60,
        daily_focus_limit_minutes=180,
        preferred_focus_windows=((time(9, 0), time(17, 30)),),
    )


def planning_preferences_to_document(
    preferences: UserPlanningPreferences,
) -> Mapping[str, Any]:
    return {
        "timezone": preferences.timezone,
        "working_day_start": preferences.working_day_start.isoformat(),
        "working_day_end": preferences.working_day_end.isoformat(),
        "minimum_block_minutes": preferences.minimum_block_minutes,
        "maximum_block_minutes": preferences.maximum_block_minutes,
        "daily_focus_limit_minutes": preferences.daily_focus_limit_minutes,
        "preferred_focus_windows": [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in preferences.preferred_focus_windows
        ],
        "schema_version": "planning_preferences_v1",
    }


def planning_preferences_from_document(
    document: Mapping[str, Any],
) -> UserPlanningPreferences:
    windows = document.get("preferred_focus_windows", ())
    return UserPlanningPreferences(
        timezone=str(document["timezone"]),
        working_day_start=time.fromisoformat(str(document["working_day_start"])),
        working_day_end=time.fromisoformat(str(document["working_day_end"])),
        minimum_block_minutes=int(document["minimum_block_minutes"]),
        maximum_block_minutes=int(document["maximum_block_minutes"]),
        daily_focus_limit_minutes=int(document["daily_focus_limit_minutes"]),
        preferred_focus_windows=tuple(
            (time.fromisoformat(str(item["start"])), time.fromisoformat(str(item["end"])))
            for item in windows
        ),
    )
