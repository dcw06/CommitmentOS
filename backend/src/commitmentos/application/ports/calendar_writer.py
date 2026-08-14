from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol

from commitmentos.application.ports.calendar_reader import CalendarEventRecord
from commitmentos.domain.actions.models import CalendarMutation


class CalendarMutationOutcomeType(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    PRECONDITION_FAILED = "precondition_failed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class CalendarMutationOutcome:
    outcome_type: CalendarMutationOutcomeType
    event: CalendarEventRecord | None
    error: Mapping[str, str] | None


class CalendarWriter(Protocol):
    async def insert_or_adopt_owned(
        self,
        mutation: CalendarMutation,
    ) -> CalendarMutationOutcome:
        ...

    async def patch_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        ...

    async def cancel_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        ...
