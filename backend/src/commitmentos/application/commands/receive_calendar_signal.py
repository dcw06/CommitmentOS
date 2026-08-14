from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from commitmentos.application.dto import CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.task_dispatcher import TaskDispatcher
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType


@dataclass(frozen=True, slots=True)
class CalendarSignal:
    channel_id: str
    resource_id: str
    resource_state: str
    message_number: str
    resource_uri: str | None
    user_id: str
    calendar_id: str


class ReceiveCalendarSignal:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        task_dispatcher: TaskDispatcher,
        clock: Clock,
        id_generator: IdGenerator,
        task_schema_version: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._task_dispatcher = task_dispatcher
        self._clock = clock
        self._id_generator = id_generator
        self._task_schema_version = task_schema_version

    async def execute(
        self,
        headers: Mapping[str, str],
        trace_id: str,
        user_id: str,
        calendar_id: str,
    ) -> CommandResult:
        signal = self._parse_signal(headers, user_id, calendar_id)
        if signal.resource_state == "sync":
            async def _snapshot_is_published(repositories: RepositorySet) -> bool:
                cursor = await repositories.sync_cursors.get(
                    signal.user_id, SourceType.CALENDAR
                )
                return cursor is not None and cursor.published_generation_id is not None

            if await self._unit_of_work.read(_snapshot_is_published):
                return CommandResult(
                    status=CommandStatus.COMPLETED,
                    identifiers={"channel_id": signal.channel_id, "handshake": "true"},
                    revision=None,
                    error_code=None,
                )
            # A new watch's handshake is also the only guaranteed signal for
            # an account that has never published authoritative Calendar
            # truth. Fall through to the normal coalesced source-sync path.
        request_id = self._sync_request_id(signal)
        now = self._clock.now()

        async def _coalesce(repositories: RepositorySet) -> None:
            existing = await repositories.sync_requests.get(request_id)
            await repositories.sync_requests.upsert(
                request_id,
                {
                    "source": SourceType.CALENDAR.value,
                    "user_id": signal.user_id,
                    "calendar_id": signal.calendar_id,
                    "status": "pending",
                    "message_number": signal.message_number,
                    "sync_generation_id": f"signal-{signal.message_number}",
                    "resource_state": signal.resource_state,
                    "initial_snapshot_required": signal.resource_state == "sync",
                    "trace_id": trace_id,
                    "created_at": (existing or {}).get("created_at", now),
                    "updated_at": now,
                },
            )

        await self._unit_of_work.run(_coalesce)
        result = await self._task_dispatcher.enqueue_source_sync(
            SourceSyncTaskV1(
                schema_version=self._task_schema_version,
                sync_request_id=request_id,
                sync_generation_id=f"signal-{signal.message_number}",
                page_sequence=0,
                source=SourceType.CALENDAR,
                user_id=signal.user_id,
                trace_id=trace_id,
            )
        )
        return CommandResult(
            status=CommandStatus.ACCEPTED,
            identifiers={
                "sync_request_id": request_id,
                "task_name": result.task_name,
            },
            revision=None,
            error_code=None,
        )

    def _parse_signal(
        self,
        headers: Mapping[str, str],
        user_id: str,
        calendar_id: str,
    ) -> CalendarSignal:
        normalized = {key.lower(): value for key, value in headers.items()}
        resource_state = normalized.get("x-goog-resource-state", "")
        if resource_state not in ("sync", "exists", "not_exists"):
            raise ValueError("invalid Calendar resource state")
        return CalendarSignal(
            channel_id=normalized["x-goog-channel-id"],
            resource_id=normalized["x-goog-resource-id"],
            resource_state=resource_state,
            message_number=normalized["x-goog-message-number"],
            resource_uri=normalized.get("x-goog-resource-uri"),
            user_id=user_id,
            calendar_id=calendar_id,
        )

    def _sync_request_id(self, signal: CalendarSignal) -> str:
        return f"{SourceType.CALENDAR.value}:{signal.user_id}"
