from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    ReconcileObservationTaskV1,
    SourceSyncTaskV1,
)


@dataclass(frozen=True, slots=True)
class TaskDispatchResult:
    task_name: str
    created: bool
    scheduled_for: datetime | None


class TaskDispatcher(Protocol):
    async def enqueue_source_sync(self, task: SourceSyncTaskV1) -> TaskDispatchResult:
        ...

    async def enqueue_reconciliation(self, task: ReconcileObservationTaskV1) -> TaskDispatchResult:
        ...

    async def enqueue_calendar_action(
        self,
        task: ExecuteCalendarActionTaskV1,
        schedule_at: datetime | None = None,
    ) -> TaskDispatchResult:
        ...
