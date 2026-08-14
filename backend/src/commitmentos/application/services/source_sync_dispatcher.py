from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from commitmentos.application.ports.task_dispatcher import TaskDispatcher, TaskDispatchResult
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType


@dataclass(frozen=True, slots=True)
class SourceSyncDispatchSummary:
    scanned: int
    queued: int
    skipped: int


class SourceSyncDispatcher:
    """Repairs the sync-request write-before-enqueue gap.

    A pending sync request whose named task was lost (crash between commit
    and enqueue) is redispatched with the same deterministic name. Calendar
    requests for Gmail and Calendar both converge through deterministic named
    source-sync tasks.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        task_dispatcher: TaskDispatcher,
        task_schema_version: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._task_dispatcher = task_dispatcher
        self._task_schema_version = task_schema_version

    async def dispatch(self, sync_request_id: str) -> TaskDispatchResult | None:
        async def _load(repositories: RepositorySet) -> Mapping[str, Any] | None:
            return await repositories.sync_requests.get(sync_request_id)

        request = await self._unit_of_work.read(_load)
        if request is None:
            return None
        task = self._task_for(request)
        if task is None:
            return None
        return await self._task_dispatcher.enqueue_source_sync(task)

    async def dispatch_pending(self, limit: int) -> SourceSyncDispatchSummary:
        async def _list(repositories: RepositorySet) -> list[Mapping[str, Any]]:
            return list(await repositories.sync_requests.list_pending(limit))

        pending = await self._unit_of_work.read(_list)
        queued = 0
        skipped = 0
        for request in pending:
            task = self._task_for(request)
            if task is None:
                skipped += 1
                continue
            await self._task_dispatcher.enqueue_source_sync(task)
            queued += 1
        return SourceSyncDispatchSummary(
            scanned=len(pending), queued=queued, skipped=skipped
        )

    def _task_for(self, request: Mapping[str, Any]) -> SourceSyncTaskV1 | None:
        source_value = request.get("source")
        if source_value not in (SourceType.GMAIL.value, SourceType.CALENDAR.value):
            return None
        user_id = request.get("user_id")
        if not user_id:
            return None
        source = SourceType(str(source_value))
        signal_version = str(
            request.get("latest_history_id")
            or request.get("message_number")
            or request.get("sync_signal_version")
            or "0"
        )
        return SourceSyncTaskV1(
            schema_version=self._task_schema_version,
            sync_request_id=request["sync_request_id"],
            sync_generation_id=request.get("sync_generation_id")
            or f"signal-{signal_version}",
            page_sequence=int(request.get("page_sequence", 0)),
            source=source,
            user_id=user_id,
            trace_id=str(request.get("trace_id", "trace-sync-repair")),
        )
