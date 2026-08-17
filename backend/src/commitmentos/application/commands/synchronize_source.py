from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from commitmentos.application.dto import CommandResult, CommandStatus, FencedLease
from commitmentos.application.ports.calendar_reader import CalendarReader, CalendarSyncPage
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.gmail_reader import (
    GmailHistoryChange,
    GmailHistoryPage,
    GmailMessage,
    GmailReader,
    SourceAuthorizationError,
    SourceCursorInvalidError,
)
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.task_dispatcher import TaskDispatcher
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import (
    ObservationFactory,
    ObservationType,
    ReconciliationStatus,
)
from commitmentos.contracts.synchronization import (
    MANIFEST_ALGORITHM_V1,
    SyncApplyCheckpoint,
    SyncGeneration,
    SyncGenerationItem,
    SyncGenerationItemStatus,
    SyncGenerationMode,
    SyncGenerationStatus,
    SyncIdFactory,
    SyncManifestHasher,
    SyncPageCheckpoint,
    SyncPublicationBarrier,
)
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType
from commitmentos.domain.actions.models import CalendarActionType, ExecutionStatus
from commitmentos.domain.planning.calendar_state import (
    CalendarEventSnapshot,
    CalendarSnapshotReducer,
)
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.models import CommitmentDemand, TimeInterval
from commitmentos.domain.planning.preferences import (
    default_user_planning_preferences,
    planning_preferences_from_document,
)

SOURCE_LEASE_SECONDS = 300
RELEVANT_LABELS = frozenset({"INBOX", "SENT"})
EXCLUDED_LABELS = frozenset({"DRAFT", "SPAM", "TRASH", "CHAT"})
# Firestore write-count overhead per bounded commit: the generation checkpoint
# update, the sync-request update, and the lease document.
TRANSACTION_WRITE_OVERHEAD = 4


class SynchronizeSource:
    """Bounded Gmail and Calendar synchronization generations (§9.1.1/§11.5).

    Each named source-sync task fetches at most one provider page. Staging,
    apply, and publication all run as separate bounded fenced transactions, so
    a worker death at any point resumes from the durable checkpoint after
    lease takeover, and the published cursor changes only in the single final
    publication transaction. Calendar applies additionally update the snapshot
    store behind the publication barrier; the cursor revision makes those
    writes authoritative only in the final publication transaction.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        gmail_reader: GmailReader,
        observation_factory: ObservationFactory,
        observation_dispatcher: ObservationDispatcher,
        task_dispatcher: TaskDispatcher,
        clock: Clock,
        id_generator: IdGenerator,
        task_schema_version: str,
        max_transaction_writes: int,
        max_estimated_transaction_bytes: int,
        max_items_per_apply_chunk: int,
        calendar_reader: CalendarReader | None = None,
        calendar_id: str = "primary",
        controlled_timezone: str = "UTC",
        calendar_full_sync_past_days: int = 30,
        calendar_full_sync_future_days: int = 365,
        publication_barrier_probe_delay_seconds: float = 0,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._gmail_reader = gmail_reader
        self._observation_factory = observation_factory
        self._observation_dispatcher = observation_dispatcher
        self._task_dispatcher = task_dispatcher
        self._clock = clock
        self._id_generator = id_generator
        self._task_schema_version = task_schema_version
        self._max_transaction_writes = max_transaction_writes
        self._max_estimated_transaction_bytes = max_estimated_transaction_bytes
        self._max_items_per_apply_chunk = max_items_per_apply_chunk
        self._calendar_reader = calendar_reader
        self._calendar_id = calendar_id
        self._controlled_timezone = controlled_timezone
        self._calendar_full_sync_past_days = calendar_full_sync_past_days
        self._calendar_full_sync_future_days = calendar_full_sync_future_days
        self._publication_barrier_probe_delay_seconds = (
            publication_barrier_probe_delay_seconds
        )
        self._hasher = SyncManifestHasher()
        self._calendar_reducer = CalendarSnapshotReducer()
        self._constraints = ConstraintEvaluator()

    async def execute(self, task: SourceSyncTaskV1) -> CommandResult:
        if task.source == SourceType.CALENDAR and self._calendar_reader is None:
            return self._result(
                CommandStatus.TERMINAL_FAILURE,
                {"source": task.source.value},
                "calendar_sync_not_configured",
            )

        fence = await self._acquire_lease(task)
        if fence is None:
            # Another worker holds the per-user source lease; Cloud Tasks
            # retries this named task after its backoff.
            return self._result(
                CommandStatus.RETRYABLE_FAILURE, {}, "source_lease_unavailable"
            )
        try:
            generation, skip_reason = await self._resolve_generation(task, fence)
            if generation is None:
                if skip_reason == "signal_already_serviced":
                    # A redelivered bootstrap after publication: release any
                    # not-yet-dispatched observations and acknowledge.
                    released = await self._release_published_observations(task.user_id)
                    return self._result(
                        CommandStatus.COMPLETED,
                        {"observations_released": str(released)},
                        None,
                    )
                return self._result(CommandStatus.NO_OP, {}, skip_reason)
            if task.page_sequence >= 1 and task.sync_generation_id != generation.sync_generation_id:
                # A continuation for a superseded generation: acknowledge.
                return self._result(
                    CommandStatus.NO_OP,
                    {"sync_generation_id": task.sync_generation_id},
                    "stale_generation_task",
                )

            if generation.status == SyncGenerationStatus.STAGING:
                try:
                    generation, more_pages = await self._stage_one_page(task, generation, fence)
                except SourceAuthorizationError:
                    await self._record_reauth_required(task)
                    return self._result(
                        CommandStatus.TERMINAL_FAILURE, {}, "reauth_required"
                    )
                except SourceCursorInvalidError:
                    await self._record_cursor_invalid(task, generation, fence)
                    await self._enqueue_full_resync(task)
                    return self._result(
                        CommandStatus.ACCEPTED, {}, "full_resync_required"
                    )
                if more_pages:
                    await self._enqueue_continuation(
                        task, generation, generation.current_page_sequence + 1
                    )
                    return self._result(
                        CommandStatus.ACCEPTED,
                        {
                            "sync_generation_id": generation.sync_generation_id,
                            "staged_pages": str(generation.current_page_sequence),
                        },
                        None,
                    )
                if (
                    task.source == SourceType.CALENDAR
                    and generation.mode == SyncGenerationMode.FULL_RESYNC
                ):
                    generation = await self._stage_calendar_tombstones(generation, fence)
                generation = await self._begin_apply(generation, fence)
                if self._publication_barrier_probe_delay_seconds:
                    # Default-off live-gate hook: keep the real durable barrier
                    # observable long enough for external refusal probes. No
                    # state is fabricated, and normal deployments use zero.
                    await asyncio.sleep(
                        self._publication_barrier_probe_delay_seconds
                    )

            if generation.status == SyncGenerationStatus.APPLYING:
                generation = await self._apply_all_chunks(generation, fence)

            if generation.status == SyncGenerationStatus.READY_TO_PUBLISH:
                followup_signal_version = await self._publish(
                    task, generation, fence
                )
                if followup_signal_version is not None:
                    await self._enqueue_signal_followup(
                        task, followup_signal_version
                    )

            released = await self._release_published_observations(task.user_id)
            return self._result(
                CommandStatus.COMPLETED,
                {
                    "sync_generation_id": generation.sync_generation_id,
                    "staged_items": str(generation.staged_item_count),
                    "observations_released": str(released),
                },
                None,
            )
        finally:
            await self._release_lease(fence)

    # ------------------------------------------------------------------
    # Lease and generation resolution
    # ------------------------------------------------------------------

    async def _acquire_lease(self, task: SourceSyncTaskV1) -> FencedLease | None:
        now = self._clock.now()
        owner = self._id_generator.new_id("syncworker")
        lease_key = f"source-sync:{task.source.value}:{task.user_id}"

        async def _acquire(repositories: RepositorySet) -> FencedLease | None:
            return await repositories.processing_leases.acquire(
                lease_key, owner, now, now + timedelta(seconds=SOURCE_LEASE_SECONDS)
            )

        return await self._unit_of_work.run(_acquire)

    async def _release_lease(self, fence: FencedLease) -> None:
        async def _release(repositories: RepositorySet) -> None:
            await repositories.processing_leases.release(
                fence.lease_key, fence.owner, fence.fencing_token
            )

        await self._unit_of_work.run(_release)

    async def _resolve_generation(
        self,
        task: SourceSyncTaskV1,
        fence: FencedLease,
    ) -> tuple[SyncGeneration | None, str]:
        now = self._clock.now()
        empty_manifest = self._hasher.empty(MANIFEST_ALGORITHM_V1)

        async def _resolve(repositories: RepositorySet) -> tuple[SyncGeneration | None, str]:
            active = await repositories.sync_generations.get_active(task.user_id, task.source)
            if active is not None:
                if active.source_fencing_token != fence.fencing_token:
                    # Takeover after the previous worker's lease expired.
                    return (
                        await repositories.sync_generations.adopt_fence(
                            active.sync_generation_id, fence, now
                        ),
                        "",
                    )
                return active, ""
            if task.page_sequence >= 1:
                # A continuation whose generation is no longer active: either
                # published (converge) or superseded (acknowledge).
                referenced = await repositories.sync_generations.get(
                    task.sync_generation_id
                )
                if (
                    referenced is not None
                    and referenced.status == SyncGenerationStatus.PUBLISHED
                ):
                    return None, "signal_already_serviced"
                return None, "stale_generation_task"
            request = await repositories.sync_requests.get(task.sync_request_id)
            if request is not None and request.get("status") not in (None, "pending"):
                # The signal that created this bootstrap task was already
                # serviced by a completed generation (at-least-once delivery).
                return None, "signal_already_serviced"
            cursor = await repositories.sync_cursors.get(task.user_id, task.source)
            if (
                task.source == SourceType.GMAIL
                and (cursor is None or cursor.published_cursor is None)
            ):
                return None, "no_published_cursor"
            cursor, generation_number = await repositories.sync_cursors.reserve_generation_number(
                task.user_id, task.source, now
            )
            full_resync = cursor.full_resync_required or (
                task.source == SourceType.CALENDAR
                and (
                    cursor.published_cursor is None
                    or cursor.published_generation_id is None
                )
            )
            mode = (
                SyncGenerationMode.FULL_RESYNC
                if full_resync
                else SyncGenerationMode.INCREMENTAL
            )
            signal_version = str(
                (request or {}).get("message_number")
                or (request or {}).get("latest_history_id")
                or ""
            )
            if task.source == SourceType.CALENDAR:
                parameters = {
                    "calendar_id": self._calendar_id,
                    "timezone": self._controlled_timezone,
                    "signal_version": signal_version,
                }
                if full_resync:
                    parameters.update(
                        {
                            "time_min": (
                                now - timedelta(days=self._calendar_full_sync_past_days)
                            ).astimezone(timezone.utc).isoformat(),
                            "time_max": (
                                now + timedelta(days=self._calendar_full_sync_future_days)
                            ).astimezone(timezone.utc).isoformat(),
                        }
                    )
                else:
                    parameters["sync_token"] = str(cursor.published_cursor)
            else:
                parameters = {"signal_version": signal_version}
                if full_resync:
                    parameters["full_resync_query"] = "{in:inbox in:sent}"
                else:
                    parameters["start_history_id"] = str(cursor.published_cursor)
            generation_id = SyncIdFactory.generation_id(
                task.source, task.user_id, cursor.revision, generation_number
            )
            generation = SyncGeneration(
                sync_generation_id=generation_id,
                sync_request_id=task.sync_request_id,
                user_id=task.user_id,
                source=task.source,
                mode=mode,
                status=SyncGenerationStatus.STAGING,
                base_published_cursor_revision=cursor.revision,
                provider_request_parameters=parameters,
                current_page_sequence=0,
                next_page_token=None,
                candidate_next_cursor=None,
                staged_manifest=empty_manifest,
                applied_manifest=empty_manifest,
                page_count=0,
                staged_item_count=0,
                applied_item_count=0,
                outstanding_chunk_id=None,
                full_sync_tombstones_complete=not (
                    full_resync and task.source == SourceType.CALENDAR
                ),
                source_lease_key=fence.lease_key,
                source_lease_owner=fence.owner,
                source_fencing_token=fence.fencing_token,
                source_lease_expires_at=fence.expires_at,
                created_at=now,
                updated_at=now,
                published_at=None,
            )
            created = await repositories.sync_generations.create(
                generation, cursor.revision, fence
            )
            if not created:
                return await repositories.sync_generations.get(generation_id), ""
            # Status stays "pending" until publication so a crash anywhere in
            # the generation leaves the request repairable by maintenance;
            # the recorded generation id makes the repair task resume it.
            await repositories.sync_requests.upsert(
                task.sync_request_id,
                {
                    "source": task.source.value,
                    "user_id": task.user_id,
                    "status": "pending",
                    "sync_generation_id": generation_id,
                    "updated_at": now,
                },
            )
            return generation, ""

        return await self._unit_of_work.run(_resolve)

    # ------------------------------------------------------------------
    # Staging: one provider page per task invocation
    # ------------------------------------------------------------------

    async def _stage_one_page(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> tuple[SyncGeneration, bool]:
        if task.page_sequence >= 1 and task.page_sequence <= generation.current_page_sequence:
            # Redelivered page task whose checkpoint already committed.
            return generation, generation.next_page_token is not None

        if task.source == SourceType.CALENDAR:
            assert self._calendar_reader is not None
            parameters = generation.provider_request_parameters
            calendar_page = await self._calendar_reader.sync_events(
                calendar_id=parameters["calendar_id"],
                sync_token=parameters.get("sync_token"),
                page_token=generation.next_page_token,
                time_min=self._parse_datetime(parameters.get("time_min")),
                time_max=self._parse_datetime(parameters.get("time_max")),
            )
            items = self._normalize_calendar_page(task, generation, calendar_page)
            next_page_token = calendar_page.next_page_token
            candidate_next_cursor = calendar_page.next_sync_token
        else:
            if generation.mode == SyncGenerationMode.FULL_RESYNC:
                mailbox_page = await self._gmail_reader.list_messages(
                    task.user_id, generation.next_page_token
                )
                gmail_page = GmailHistoryPage(
                    changes=(
                        GmailHistoryChange(
                            # A synthetic change carries provider IDs only;
                            # immutable content hashes remain the item version.
                            history_id=mailbox_page.latest_history_id or "full-resync",
                            message_ids=mailbox_page.message_ids,
                            label_ids=(),
                        ),
                    ) if mailbox_page.message_ids else (),
                    next_page_token=mailbox_page.next_page_token,
                    latest_history_id=(
                        mailbox_page.latest_history_id
                        or generation.candidate_next_cursor
                        or ""
                    ),
                )
            else:
                start_history_id = generation.provider_request_parameters[
                    "start_history_id"
                ]
                gmail_page = await self._gmail_reader.list_history(
                    task.user_id, start_history_id, generation.next_page_token
                )
            items = await self._normalize_gmail_page(task, generation, gmail_page)
            next_page_token = gmail_page.next_page_token
            candidate_next_cursor = gmail_page.latest_history_id

        chunk_manifests = []
        for chunk in self._chunks_by_budget(items):
            chunk_manifest = self._hasher.for_items(tuple(chunk), MANIFEST_ALGORITHM_V1)
            chunk_manifests.append(chunk_manifest)

            async def _stage(
                repositories: RepositorySet,
                chunk: Sequence[SyncGenerationItem] = chunk,
                chunk_manifest: Any = chunk_manifest,
            ) -> int:
                return await repositories.sync_generation_items.stage_chunk(
                    generation, chunk, chunk_manifest, fence
                )

            await self._unit_of_work.run_fenced(fence, _stage)

        page_manifest = self._hasher.for_items(tuple(items), MANIFEST_ALGORITHM_V1)
        aggregate = self._hasher.combine(generation.staged_manifest, page_manifest)
        final_page = next_page_token is None
        if final_page and not candidate_next_cursor:
            raise ValueError("the final provider page must supply a candidate cursor")
        checkpoint = SyncPageCheckpoint(
            sync_generation_id=generation.sync_generation_id,
            page_sequence=generation.current_page_sequence + 1,
            staged_item_count=len(items),
            committed_write_count=len(items) + TRANSACTION_WRITE_OVERHEAD,
            estimated_commit_bytes=sum(self._estimate_item_bytes(item) for item in items),
            page_manifest=page_manifest,
            aggregate_staged_manifest=aggregate,
            next_page_token=next_page_token,
            candidate_next_cursor=(
                candidate_next_cursor
                if final_page
                or (
                    task.source == SourceType.GMAIL
                    and generation.mode == SyncGenerationMode.FULL_RESYNC
                )
                else None
            ),
            final_provider_page=final_page,
            committed_at=self._clock.now(),
        )

        async def _checkpoint(repositories: RepositorySet) -> SyncGeneration:
            return await repositories.sync_generations.record_page_checkpoint(
                checkpoint, SyncGenerationStatus.STAGING, fence
            )

        updated = await self._unit_of_work.run_fenced(fence, _checkpoint)
        return updated, not final_page

    def _normalize_calendar_page(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        page: CalendarSyncPage,
    ) -> list[SyncGenerationItem]:
        now = self._clock.now()
        items: list[SyncGenerationItem] = []
        for record in page.events:
            event = record.payload
            normalized_payload: dict[str, Any] = {
                "user_id": task.user_id,
                "calendar_id": record.calendar_id,
                "id": record.event_id,
                "etag": record.etag,
                "status": record.status,
                "payload_hash": record.payload_hash,
                "default_timezone": generation.provider_request_parameters.get(
                    "timezone", self._controlled_timezone
                ),
            }
            for field in (
                "start",
                "end",
                "transparency",
                "recurringEventId",
                "originalStartTime",
                "extendedProperties",
            ):
                if field in event:
                    normalized_payload[field] = event[field]
            external_version = str(record.etag or record.payload_hash)
            items.append(
                SyncGenerationItem(
                    sync_generation_item_id=SyncIdFactory.item_id(
                        generation.sync_generation_id,
                        record.event_id,
                        external_version,
                    ),
                    sync_generation_id=generation.sync_generation_id,
                    page_sequence=generation.current_page_sequence + 1,
                    chunk_sequence=0,
                    source=SourceType.CALENDAR,
                    external_id=record.event_id,
                    external_version=external_version,
                    payload_hash=record.payload_hash,
                    normalized_kind="calendar_event",
                    normalized_payload=normalized_payload,
                    status=SyncGenerationItemStatus.STAGED,
                    staged_at=now,
                    applied_at=None,
                )
            )
        return items

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    async def _stage_calendar_tombstones(
        self,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> SyncGeneration:
        """Stage synthetic deletions for rows absent from a bounded full read.

        Provider events and synthetic tombstones share the same manifest and
        apply path, so a crash cannot publish a partially replaced snapshot.
        """
        present_event_ids: set[str] = set()
        after_item_id: str | None = None
        while True:
            async def _items(repositories: RepositorySet) -> list[SyncGenerationItem]:
                return list(
                    await repositories.sync_generation_items.list_for_generation(
                        generation.sync_generation_id, after_item_id, 250
                    )
                )

            page = await self._unit_of_work.read(_items)
            present_event_ids.update(
                item.external_id
                for item in page
                if item.normalized_kind == "calendar_event"
            )
            if len(page) < 250:
                break
            after_item_id = page[-1].sync_generation_item_id

        previous: list[CalendarEventSnapshot] = []
        after_snapshot_id: str | None = None
        while True:
            async def _snapshots(
                repositories: RepositorySet,
            ) -> list[CalendarEventSnapshot]:
                return list(
                    await repositories.calendar_snapshots.list_for_calendar(
                        generation.user_id,
                        generation.provider_request_parameters["calendar_id"],
                        after_snapshot_id,
                        250,
                    )
                )

            page_snapshots = await self._unit_of_work.read(_snapshots)
            previous.extend(page_snapshots)
            if len(page_snapshots) < 250:
                break
            after_snapshot_id = page_snapshots[-1].calendar_snapshot_id

        now = self._clock.now()
        tombstones: list[SyncGenerationItem] = []
        for snapshot in previous:
            if snapshot.is_tombstone or snapshot.calendar_event_id in present_event_ids:
                continue
            version = f"full-resync-tombstone:{snapshot.observed_payload_hash}"
            payload_hash = SyncIdFactory.item_id(
                generation.sync_generation_id,
                snapshot.calendar_event_id,
                version,
            )
            tombstones.append(
                SyncGenerationItem(
                    sync_generation_item_id=payload_hash,
                    sync_generation_id=generation.sync_generation_id,
                    page_sequence=generation.current_page_sequence + 1,
                    chunk_sequence=0,
                    source=SourceType.CALENDAR,
                    external_id=snapshot.calendar_event_id,
                    external_version=version,
                    payload_hash=payload_hash,
                    normalized_kind="calendar_tombstone",
                    normalized_payload={
                        "user_id": generation.user_id,
                        "calendar_id": generation.provider_request_parameters["calendar_id"],
                        "id": snapshot.calendar_event_id,
                        "status": "cancelled",
                        "is_tombstone": True,
                        "payload_hash": payload_hash,
                        "default_timezone": generation.provider_request_parameters.get(
                            "timezone", self._controlled_timezone
                        ),
                    },
                    status=SyncGenerationItemStatus.STAGED,
                    staged_at=now,
                    applied_at=None,
                )
            )
        if not tombstones:
            return generation
        for chunk in self._chunks_by_budget(tombstones):
            manifest = self._hasher.for_items(tuple(chunk), MANIFEST_ALGORITHM_V1)

            async def _stage(
                repositories: RepositorySet,
                chunk: Sequence[SyncGenerationItem] = chunk,
                manifest: Any = manifest,
            ) -> int:
                return await repositories.sync_generation_items.stage_chunk(
                    generation, chunk, manifest, fence
                )

            await self._unit_of_work.run_fenced(fence, _stage)
        page_manifest = self._hasher.for_items(tuple(tombstones), MANIFEST_ALGORITHM_V1)
        checkpoint = SyncPageCheckpoint(
            sync_generation_id=generation.sync_generation_id,
            page_sequence=generation.current_page_sequence + 1,
            staged_item_count=len(tombstones),
            committed_write_count=len(tombstones) + TRANSACTION_WRITE_OVERHEAD,
            estimated_commit_bytes=sum(
                self._estimate_item_bytes(item) for item in tombstones
            ),
            page_manifest=page_manifest,
            aggregate_staged_manifest=self._hasher.combine(
                generation.staged_manifest, page_manifest
            ),
            next_page_token=None,
            candidate_next_cursor=None,
            final_provider_page=True,
            committed_at=now,
        )

        async def _checkpoint(repositories: RepositorySet) -> SyncGeneration:
            return await repositories.sync_generations.record_page_checkpoint(
                checkpoint, SyncGenerationStatus.STAGING, fence
            )

        return await self._unit_of_work.run_fenced(fence, _checkpoint)

    async def _normalize_gmail_page(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        page: GmailHistoryPage,
    ) -> list[SyncGenerationItem]:
        message_ids: list[str] = []
        seen: set[str] = set()
        for change in page.changes:
            for message_id in change.message_ids:
                if message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)

        items: list[SyncGenerationItem] = []
        now = self._clock.now()
        for message_id in message_ids:
            message = await self._gmail_reader.get_message(task.user_id, message_id)
            if message is None:
                # The message vanished between the history record and the
                # fetch (deleted or a discarded draft): nothing to observe.
                continue
            if not self._is_relevant(message):
                continue
            item_id = SyncIdFactory.item_id(
                generation.sync_generation_id, message.message_id, message.payload_hash
            )
            items.append(
                SyncGenerationItem(
                    sync_generation_item_id=item_id,
                    sync_generation_id=generation.sync_generation_id,
                    page_sequence=generation.current_page_sequence + 1,
                    chunk_sequence=0,
                    source=SourceType.GMAIL,
                    external_id=message.message_id,
                    external_version=message.payload_hash,
                    payload_hash=message.payload_hash,
                    normalized_kind="gmail_message",
                    # Minimal normalized fields only (plan §13.3): identifiers,
                    # time, labels, subject. Message bodies are never staged.
                    normalized_payload={
                        "message_id": message.message_id,
                        "thread_id": message.thread_id,
                        "internal_date": message.internal_date.isoformat(),
                        "label_ids": list(message.label_ids),
                        "subject": message.headers.get("subject", ""),
                    },
                    status=SyncGenerationItemStatus.STAGED,
                    staged_at=now,
                    applied_at=None,
                )
            )
        return items

    @staticmethod
    def _is_relevant(message: GmailMessage) -> bool:
        labels = set(message.label_ids)
        if labels & EXCLUDED_LABELS:
            return False
        return bool(labels & RELEVANT_LABELS)

    def _chunks_by_budget(
        self,
        items: Sequence[SyncGenerationItem],
    ) -> list[list[SyncGenerationItem]]:
        """Split staged items so each commit stays under the write and byte
        budgets (writes and bytes are counted per item, never per page)."""
        chunks: list[list[SyncGenerationItem]] = []
        current: list[SyncGenerationItem] = []
        current_bytes = 0
        write_budget = max(1, self._max_transaction_writes - TRANSACTION_WRITE_OVERHEAD)
        for item in items:
            item_bytes = self._estimate_item_bytes(item)
            if current and (
                len(current) >= write_budget
                or current_bytes + item_bytes > self._max_estimated_transaction_bytes
            ):
                chunks.append(current)
                current = []
                current_bytes = 0
            current.append(item)
            current_bytes += item_bytes
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _estimate_item_bytes(item: SyncGenerationItem) -> int:
        payload = json.dumps(dict(item.normalized_payload), sort_keys=True, default=str)
        # Document fields plus index entries; doubled as a conservative margin.
        return 2 * (len(payload) + 512)

    # ------------------------------------------------------------------
    # Apply: bounded chunks over staged items
    # ------------------------------------------------------------------

    async def _begin_apply(
        self,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> SyncGeneration:
        now = self._clock.now()
        staged_manifest = generation.staged_manifest
        barrier = SyncPublicationBarrier(
            user_id=generation.user_id,
            source=generation.source,
            sync_generation_id=generation.sync_generation_id,
            base_published_cursor_revision=generation.base_published_cursor_revision,
            activated_at=now,
        )

        async def _begin(repositories: RepositorySet) -> SyncGeneration:
            # The first apply transaction activates the publication barrier
            # (architecture §11.5 step 4) atomically with entering `applying`.
            await repositories.sync_cursors.activate_publication_barrier(
                barrier, generation.base_published_cursor_revision, fence
            )
            return await repositories.sync_generations.begin_applying(
                generation.sync_generation_id, staged_manifest, fence, now
            )

        return await self._unit_of_work.run_fenced(fence, _begin)

    async def _apply_all_chunks(
        self,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> SyncGeneration:
        chunk_size = min(
            self._max_items_per_apply_chunk,
            max(1, self._max_transaction_writes - TRANSACTION_WRITE_OVERHEAD),
        )
        chunk_sequence = 0
        while True:
            async def _load(repositories: RepositorySet) -> list[SyncGenerationItem]:
                return list(
                    await repositories.sync_generation_items.list_staged_for_apply(
                        generation.sync_generation_id, None, chunk_size
                    )
                )

            items = await self._unit_of_work.read(_load)
            if not items:
                break
            chunk_sequence += 1
            chunk_manifest = self._hasher.for_items(tuple(items), MANIFEST_ALGORITHM_V1)

            async def _apply(
                repositories: RepositorySet,
                items: Sequence[SyncGenerationItem] = items,
                chunk_manifest: Any = chunk_manifest,
                chunk_sequence: int = chunk_sequence,
            ) -> SyncGeneration:
                current = await repositories.sync_generations.get(
                    generation.sync_generation_id
                )
                if current is None:
                    raise ValueError("generation disappeared during apply")
                for item in items:
                    if item.source == SourceType.CALENDAR:
                        observation = await self._apply_calendar_item(
                            repositories, item, generation
                        )
                    else:
                        observation = self._observation_for(item, generation.user_id)
                    if observation is not None:
                        await repositories.observations.create(observation)
                checkpoint = SyncApplyCheckpoint(
                    sync_generation_id=generation.sync_generation_id,
                    chunk_sequence=chunk_sequence,
                    first_item_id=items[0].sync_generation_item_id,
                    last_item_id=items[-1].sync_generation_item_id,
                    applied_item_count=len(items),
                    chunk_manifest=chunk_manifest,
                    aggregate_applied_manifest=self._hasher.combine(
                        current.applied_manifest, chunk_manifest
                    ),
                    full_sync_tombstones_complete=current.full_sync_tombstones_complete,
                    committed_at=self._clock.now(),
                )
                await repositories.sync_generation_items.mark_applied(
                    generation.sync_generation_id,
                    [item.sync_generation_item_id for item in items],
                    checkpoint,
                    fence,
                )
                return await repositories.sync_generations.record_apply_checkpoint(
                    checkpoint, SyncGenerationStatus.APPLYING, fence
                )

            generation = await self._unit_of_work.run_fenced(fence, _apply)

        now = self._clock.now()

        if (
            generation.mode == SyncGenerationMode.FULL_RESYNC
            and not generation.full_sync_tombstones_complete
        ):
            async def _tombstones_complete(repositories: RepositorySet) -> SyncGeneration:
                return await repositories.sync_generations.mark_full_sync_tombstones_complete(
                    generation.sync_generation_id, fence, now
                )

            generation = await self._unit_of_work.run_fenced(
                fence, _tombstones_complete
            )

        async def _ready(repositories: RepositorySet) -> SyncGeneration:
            current = await repositories.sync_generations.get(generation.sync_generation_id)
            if current is None:
                raise ValueError("generation disappeared before publication")
            return await repositories.sync_generations.mark_ready_to_publish(
                generation.sync_generation_id,
                current.staged_manifest,
                current.applied_manifest,
                fence,
                now,
            )

        return await self._unit_of_work.run_fenced(fence, _ready)

    async def _apply_calendar_item(
        self,
        repositories: RepositorySet,
        item: SyncGenerationItem,
        generation: SyncGeneration,
    ) -> Any | None:
        cursor = await repositories.sync_cursors.get(
            generation.user_id, SourceType.CALENDAR
        )
        if (
            cursor is None
            or cursor.publish_in_progress_generation_id
            != generation.sync_generation_id
        ):
            raise ValueError("calendar snapshot apply requires the publication barrier")
        previous = await repositories.calendar_snapshots.get(
            generation.provider_request_parameters["calendar_id"], item.external_id
        )
        snapshot = self._calendar_reducer.reduce_change(
            previous,
            item.normalized_payload,
            source_observation_id=SyncIdFactory.item_id(
                generation.sync_generation_id,
                item.external_id,
                item.external_version,
            ),
            calendar_state_revision=cursor.calendar_state_revision + 1,
            observed_at=self._clock.now(),
        )
        if (
            previous is not None
            and previous.observed_payload_hash == snapshot.observed_payload_hash
            and previous.is_tombstone == snapshot.is_tombstone
        ):
            await repositories.calendar_snapshots.save(snapshot)
            return None
        observation_type, echo = await self._classify_calendar_change(
            repositories, previous, snapshot
        )
        await repositories.calendar_snapshots.save(snapshot)
        return self._observation_factory.source_change(
            observation_type=observation_type,
            user_id=generation.user_id,
            producer_id=f"{snapshot.calendar_id}:{snapshot.calendar_event_id}",
            producer_version=item.external_version,
            source=SourceType.CALENDAR.value,
            external_id=item.external_id,
            external_version=item.external_version,
            payload_hash=item.payload_hash,
            source_reference={
                "calendar_id": snapshot.calendar_id,
                "calendar_event_id": snapshot.calendar_event_id,
                "commitment_id": snapshot.observed_commitment_id or "",
                "work_block_id": snapshot.observed_work_block_id or "",
            },
            safe_metadata={
                "classification": observation_type.value,
                "commitment_id": snapshot.observed_commitment_id or "",
                "work_block_id": snapshot.observed_work_block_id or "",
                "previous_start": (
                    previous.observed_start.isoformat()
                    if previous and previous.observed_start
                    else ""
                ),
                "previous_end": (
                    previous.observed_end.isoformat()
                    if previous and previous.observed_end
                    else ""
                ),
                "observed_start": (
                    snapshot.observed_start.isoformat() if snapshot.observed_start else ""
                ),
                "observed_end": (
                    snapshot.observed_end.isoformat() if snapshot.observed_end else ""
                ),
                "sync_generation_id": item.sync_generation_id,
                "echo_suppressed": echo,
            },
            observed_at=self._clock.now(),
            trace_id=f"sync-{item.sync_generation_id[:12]}",
            reconciliation_status=(
                ReconciliationStatus.IGNORED
                if echo
                else ReconciliationStatus.STAGED
            ),
        )

    async def _classify_calendar_change(
        self,
        repositories: RepositorySet,
        previous: CalendarEventSnapshot | None,
        current: CalendarEventSnapshot,
    ) -> tuple[ObservationType, bool]:
        owned = current.is_app_owned() or bool(previous and previous.is_app_owned())
        if owned and current.is_tombstone:
            if current.observed_work_block_id:
                actions = await repositories.outbox.list_for_work_block(
                    current.user_id,
                    current.observed_work_block_id,
                    10,
                )
                if any(
                    action.execution_status == ExecutionStatus.SUCCEEDED
                    and action.mutation.action_type == CalendarActionType.CANCEL
                    and action.mutation.calendar_event_id == current.calendar_event_id
                    for action in actions
                ):
                    return ObservationType.CALENDAR_APP_ECHO, True
            # A stale etag from an earlier insert/patch is not proof that a
            # later tombstone is our echo. Only a succeeded cancel suppresses
            # a deletion; every other owned tombstone requires one decision.
            return ObservationType.CALENDAR_USER_DELETION, False
        if owned and current.observed_work_block_id:
            actions = await repositories.outbox.list_for_work_block(
                current.user_id, current.observed_work_block_id, 10
            )
            for action in actions:
                if action.execution_status != ExecutionStatus.SUCCEEDED:
                    continue
                response = action.mutation_response
                echo_matches = response is not None and (
                    (
                        response.mutation_response_etag is not None
                        and response.mutation_response_etag
                        == current.observed_event_etag
                    )
                    or (
                        bool(response.mutation_response_payload_hash)
                        and response.mutation_response_payload_hash
                        == current.observed_payload_hash
                    )
                )
                if echo_matches:
                    return ObservationType.CALENDAR_APP_ECHO, True
        timing_changed = previous is None or (
            previous.observed_start != current.observed_start
            or previous.observed_end != current.observed_end
        )
        if owned and timing_changed:
            valid = await self._calendar_move_satisfies_hard_constraints(
                repositories, current
            )
            return (
                ObservationType.CALENDAR_USER_MOVE_VALID
                if valid
                else ObservationType.CALENDAR_USER_MOVE_INVALID,
                False,
            )
        if not owned:
            return ObservationType.CALENDAR_ENVIRONMENTAL_DISRUPTION, False
        return ObservationType.CALENDAR_EVENT_CHANGED, False

    async def _calendar_move_satisfies_hard_constraints(
        self,
        repositories: RepositorySet,
        snapshot: CalendarEventSnapshot,
    ) -> bool:
        if (
            snapshot.observed_start is None
            or snapshot.observed_end is None
            or snapshot.observed_work_block_id is None
            or snapshot.observed_commitment_id is None
        ):
            return False
        try:
            interval = TimeInterval(snapshot.observed_start, snapshot.observed_end)
        except ValueError:
            return False
        work_block = await repositories.work_blocks.get(snapshot.observed_work_block_id)
        commitment = await repositories.commitments.get(snapshot.observed_commitment_id)
        if work_block is None or commitment is None:
            return False
        user = await repositories.users.get(snapshot.user_id)
        stored_preferences = (user or {}).get("planning_preferences")
        preferences = (
            planning_preferences_from_document(stored_preferences)
            if isinstance(stored_preferences, Mapping)
            else default_user_planning_preferences(self._controlled_timezone)
        )
        calendar_rows: list[CalendarEventSnapshot] = []
        after_snapshot_id: str | None = None
        while True:
            page = list(
                await repositories.calendar_snapshots.list_for_calendar(
                    snapshot.user_id,
                    snapshot.calendar_id,
                    after_snapshot_id,
                    250,
                )
            )
            calendar_rows.extend(page)
            if len(page) < 250:
                break
            after_snapshot_id = page[-1].calendar_snapshot_id
        occupied = [
            candidate.interval()
            for candidate in calendar_rows
            if candidate.contributes_busy_time()
            and candidate.observed_work_block_id != snapshot.observed_work_block_id
            and candidate.interval() is not None
        ]
        daily_focus = [
            candidate.interval()
            for candidate in calendar_rows
            if candidate.is_app_owned()
            and candidate.contributes_busy_time()
            and candidate.observed_work_block_id != snapshot.observed_work_block_id
            and candidate.interval() is not None
        ]
        demand = CommitmentDemand(
            commitment_id=commitment.commitment_id,
            commitment_revision=commitment.revision,
            plan_revision=commitment.plan_revision,
            deadline=commitment.deadline.value,
            remaining_minutes=work_block.duration_minutes,
            explicit_priority=0,
            created_at=commitment.created_at,
        )
        return self._constraints.is_valid(
            interval,
            demand,
            preferences,
            [value for value in occupied if value is not None],
            self._clock.now(),
            [value for value in daily_focus if value is not None],
        )

    def _observation_for(self, item: SyncGenerationItem, user_id: str) -> Any:
        payload = item.normalized_payload
        return self._observation_factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=user_id,
            producer_id=f"{user_id}:{item.external_id}",
            producer_version=item.external_version,
            source=SourceType.GMAIL.value,
            external_id=item.external_id,
            external_version=item.external_version,
            payload_hash=item.payload_hash,
            source_reference={
                "thread_id": str(payload.get("thread_id", "")),
                "message_id": item.external_id,
            },
            safe_metadata={
                "subject": str(payload.get("subject", "")),
                "internal_date": str(payload.get("internal_date", "")),
                "label_ids": list(payload.get("label_ids", [])),
                "sync_generation_id": item.sync_generation_id,
            },
            observed_at=self._clock.now(),
            trace_id=f"sync-{item.sync_generation_id[:12]}",
            reconciliation_status=ReconciliationStatus.STAGED,
        )

    # ------------------------------------------------------------------
    # Publication and release
    # ------------------------------------------------------------------

    async def _publish(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> str | None:
        published_at = self._clock.now()

        async def _publish_txn(repositories: RepositorySet) -> str | None:
            current = await repositories.sync_generations.get(generation.sync_generation_id)
            if current is None:
                raise ValueError("generation disappeared before publication")
            await repositories.sync_cursors.publish_generation(
                current,
                SyncGenerationStatus.READY_TO_PUBLISH,
                current.staged_manifest,
                current.applied_manifest,
                fence,
                published_at,
            )
            request = await repositories.sync_requests.get(task.sync_request_id)
            latest_signal_version = str(
                (request or {}).get("message_number")
                or (request or {}).get("latest_history_id")
                or ""
            )
            captured_signal_version = str(
                current.provider_request_parameters.get("signal_version", "")
            )
            followup = (
                latest_signal_version
                if task.source == SourceType.CALENDAR
                and latest_signal_version
                and latest_signal_version != captured_signal_version
                else None
            )
            await repositories.sync_requests.upsert(
                task.sync_request_id,
                {
                    "status": "pending" if followup else "published",
                    "sync_generation_id": (
                        f"signal-{followup}"
                        if followup
                        else generation.sync_generation_id
                    ),
                    "published_at": published_at,
                    "updated_at": published_at,
                },
            )
            return followup

        return await self._unit_of_work.run_fenced(fence, _publish_txn)

    async def _enqueue_signal_followup(
        self,
        task: SourceSyncTaskV1,
        signal_version: str,
    ) -> None:
        await self._task_dispatcher.enqueue_source_sync(
            SourceSyncTaskV1(
                schema_version=self._task_schema_version,
                sync_request_id=task.sync_request_id,
                sync_generation_id=f"signal-{signal_version}",
                page_sequence=0,
                source=task.source,
                user_id=task.user_id,
                trace_id=task.trace_id,
            )
        )

    async def _release_published_observations(self, user_id: str) -> int:
        """Flip staged observations to pending in bounded batches after
        publication, then dispatch them (architecture §11.5 step 7)."""
        released = 0
        batch_size = 50
        while True:
            async def _release(repositories: RepositorySet) -> list[str]:
                staged = await repositories.observations.list_staged_for_release(
                    user_id, batch_size
                )
                eligible: list[str] = []
                for observation in staged:
                    generation_id = observation.safe_metadata.get("sync_generation_id")
                    if generation_id:
                        generation = await repositories.sync_generations.get(generation_id)
                        if (
                            generation is None
                            or generation.status != SyncGenerationStatus.PUBLISHED
                        ):
                            continue
                    await repositories.observations.set_reconciliation_status(
                        observation.observation_id,
                        {ReconciliationStatus.STAGED},
                        ReconciliationStatus.PENDING,
                        observation.dispatch_generation,
                        None,
                        None,
                        None,
                    )
                    eligible.append(observation.observation_id)
                return eligible

            observation_ids = await self._unit_of_work.run(_release)
            if not observation_ids:
                break
            for observation_id in observation_ids:
                try:
                    await self._observation_dispatcher.dispatch(observation_id)
                except Exception:  # noqa: BLE001 - dispatch gap is repairable
                    pass
            released += len(observation_ids)
            if len(observation_ids) < batch_size:
                break
        return released

    # ------------------------------------------------------------------
    # Failure states
    # ------------------------------------------------------------------

    async def _record_reauth_required(self, task: SourceSyncTaskV1) -> None:
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            await repositories.sync_requests.upsert(
                task.sync_request_id,
                {
                    "status": "reauth_required",
                    "auth_error": "invalid_grant",
                    "auth_failed_at": now,
                    "updated_at": now,
                },
            )

        await self._unit_of_work.run(_record)

    async def _record_cursor_invalid(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        fence: FencedLease,
    ) -> None:
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            await repositories.sync_cursors.mark_full_resync_required(
                task.user_id,
                task.source,
                generation.base_published_cursor_revision,
                fence,
                now,
            )
            await repositories.sync_requests.upsert(
                task.sync_request_id,
                {"status": "full_resync_required", "updated_at": now},
            )
            await repositories.sync_generations.abandon(
                generation.sync_generation_id, fence, now
            )

        await self._unit_of_work.run_fenced(fence, _record)

    async def _enqueue_full_resync(self, task: SourceSyncTaskV1) -> None:
        request_id = f"{task.source.value}:{task.user_id}:full-resync"
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            await repositories.sync_requests.upsert(
                request_id,
                {
                    "source": task.source.value,
                    "user_id": task.user_id,
                    "calendar_id": (
                        self._calendar_id if task.source == SourceType.CALENDAR else None
                    ),
                    "status": "pending",
                    "full_resync": True,
                    "trace_id": task.trace_id,
                    "updated_at": now,
                },
            )

        await self._unit_of_work.run(_record)
        await self._task_dispatcher.enqueue_source_sync(
            SourceSyncTaskV1(
                schema_version=self._task_schema_version,
                sync_request_id=request_id,
                sync_generation_id="full-resync",
                page_sequence=0,
                source=task.source,
                user_id=task.user_id,
                trace_id=task.trace_id,
            )
        )

    async def _enqueue_continuation(
        self,
        task: SourceSyncTaskV1,
        generation: SyncGeneration,
        next_page_sequence: int,
    ) -> None:
        await self._task_dispatcher.enqueue_source_sync(
            SourceSyncTaskV1(
                schema_version=self._task_schema_version,
                sync_request_id=task.sync_request_id,
                sync_generation_id=generation.sync_generation_id,
                page_sequence=next_page_sequence,
                source=task.source,
                user_id=task.user_id,
                trace_id=task.trace_id,
            )
        )

    @staticmethod
    def _result(
        status: CommandStatus,
        identifiers: Mapping[str, str],
        error_code: str | None,
    ) -> CommandResult:
        return CommandResult(
            status=status,
            identifiers=dict(identifiers),
            revision=None,
            error_code=error_code,
        )
