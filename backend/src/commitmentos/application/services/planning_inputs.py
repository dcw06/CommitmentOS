from __future__ import annotations

from commitmentos.application.ports.calendar_reader import CalendarReader
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.domain.planning.calendar_state import CalendarStateSnapshot
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    TimeInterval,
    UserPlanningPreferences,
)
from commitmentos.domain.planning.preferences import (
    default_user_planning_preferences,
    planning_preferences_from_document,
    planning_preferences_to_document,
)


class PlanningInputReader:
    """Loads authoritative Phase 3A inputs without making planning decisions."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        calendar_reader: CalendarReader,
        clock: Clock,
        calendar_id: str,
        controlled_timezone: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._calendar_reader = calendar_reader
        self._clock = clock
        self._calendar_id = calendar_id
        self._controlled_timezone = controlled_timezone

    async def get_preferences(self, user_id: str) -> UserPlanningPreferences:
        async def _get_or_create(repositories: RepositorySet) -> UserPlanningPreferences:
            current = await repositories.users.get(user_id)
            user = dict(current or {})
            stored = user.get("planning_preferences")
            if isinstance(stored, dict):
                return planning_preferences_from_document(stored)
            defaults = default_user_planning_preferences(self._controlled_timezone)
            user["planning_preferences"] = dict(planning_preferences_to_document(defaults))
            user["planning_preferences_revision"] = 1
            user["planning_preferences_updated_at"] = self._clock.now()
            await repositories.users.save(user_id, user)
            return defaults

        return await self._unit_of_work.run(_get_or_create)

    async def list_busy_intervals(
        self,
        user_id: str,
        planning_horizon: TimeInterval,
    ) -> tuple[CalendarBusyInterval, ...]:
        preferences = await self.get_preferences(user_id)
        return await self._calendar_reader.list_busy_intervals(
            self._calendar_id,
            planning_horizon,
            preferences.timezone,
        )

    async def load_calendar_state(
        self,
        user_id: str,
        planning_horizon: TimeInterval,
    ) -> CalendarStateSnapshot:
        async def _load(repositories: RepositorySet) -> CalendarStateSnapshot:
            return await repositories.calendar_snapshots.load_consistent_state(
                user_id,
                self._calendar_id,
                planning_horizon.start,
                planning_horizon.end,
            )

        return await self._unit_of_work.read(_load)
