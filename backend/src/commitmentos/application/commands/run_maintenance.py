from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Callable, Mapping

from commitmentos.application.dto import CommandResult, CommandStatus
from commitmentos.application.ports.calendar_reader import CalendarReader
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.gmail_reader import (
    GmailReader,
    SourceAuthorizationError,
)
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.task_dispatcher import TaskDispatcher
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.application.services.outbox_dispatcher import OutboxDispatcher
from commitmentos.application.services.source_sync_dispatcher import SourceSyncDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.commitments.models import RiskLevel
from commitmentos.domain.planning.models import PlannerRunStatus
from commitmentos.domain.progress.models import UserEditState, WorkBlockExecutionState
from commitmentos.domain.progress.service import ProgressCalculator
from commitmentos.domain.shared.types import CanonicalEncoder

GMAIL_WATCH_LABELS = ("INBOX", "SENT")
# Catch-up when no Gmail publication happened within this window even though
# a cursor exists (plan §9.1: periodic history.list fallback when no
# notification arrives).
CATCH_UP_QUIET_WINDOW = timedelta(hours=6)
STUCK_GENERATION_WINDOW = timedelta(minutes=5)

logger = logging.getLogger("commitmentos.safety")


@dataclass(frozen=True, slots=True)
class _SafetyCandidate:
    producer_id: str
    producer_version: str
    metadata: Mapping[str, Any]


class MaintenanceKind(StrEnum):
    RENEW_WATCHES = "renew_watches"
    RECOVER_CURSORS = "recover_cursors"
    DISPATCH_PENDING = "dispatch_pending"
    SAFETY_RECONCILIATION = "safety_reconciliation"


class RunMaintenance:
    """Bounded scheduler-driven repair work.

    `dispatch_pending` repairs every write-before-enqueue gap (observations,
    outbox actions, sync requests). `renew_watches` re-arms the Gmail watch
    daily without touching the published cursor. `recover_cursors` runs the
    quiet-window Gmail catch-up. Calendar channel renewal and deterministic
    safety observations keep lifecycle, projection, drift, and recovery facts
    current without making scheduler-side planning decisions.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        observation_dispatcher: ObservationDispatcher,
        outbox_dispatcher: OutboxDispatcher,
        task_dispatcher: TaskDispatcher,
        clock: Clock,
        controlled_user_id: str,
        task_schema_version: str,
        scan_limit: int,
        source_sync_dispatcher: SourceSyncDispatcher | None = None,
        gmail_reader: GmailReader | None = None,
        gmail_pubsub_topic: str = "",
        calendar_reader: CalendarReader | None = None,
        calendar_id: str = "",
        calendar_callback_url: str = "",
        calendar_channel_token_provider: Callable[[], str] | None = None,
        id_generator: IdGenerator | None = None,
        observation_factory: ObservationFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._observation_dispatcher = observation_dispatcher
        self._outbox_dispatcher = outbox_dispatcher
        self._task_dispatcher = task_dispatcher
        self._clock = clock
        self._controlled_user_id = controlled_user_id
        self._task_schema_version = task_schema_version
        self._scan_limit = scan_limit
        self._source_sync_dispatcher = source_sync_dispatcher or SourceSyncDispatcher(
            unit_of_work, task_dispatcher, task_schema_version
        )
        self._gmail_reader = gmail_reader
        self._gmail_pubsub_topic = gmail_pubsub_topic
        self._calendar_reader = calendar_reader
        self._calendar_id = calendar_id
        self._calendar_callback_url = calendar_callback_url
        self._calendar_channel_token_provider = calendar_channel_token_provider
        self._id_generator = id_generator
        self._observation_factory = observation_factory or ObservationFactory()
        self._progress = ProgressCalculator()

    async def execute(
        self,
        kind: MaintenanceKind,
        trace_id: str,
    ) -> CommandResult:
        if kind == MaintenanceKind.DISPATCH_PENDING:
            return await self.dispatch_pending(trace_id)
        if kind == MaintenanceKind.RENEW_WATCHES:
            return await self.renew_watches(trace_id)
        if kind == MaintenanceKind.RECOVER_CURSORS:
            return await self.recover_cursors(trace_id)
        return await self.create_safety_observations(trace_id)

    async def renew_watches(self, trace_id: str) -> CommandResult:
        """Re-arm the Gmail watch (expires every 7 days; renewed daily).

        The returned history id is informational only — renewal must never
        advance or replace the published cursor.
        """
        gmail_configured = self._gmail_reader is not None and bool(self._gmail_pubsub_topic)
        calendar_configured = all(
            (
                self._calendar_reader is not None,
                self._calendar_id,
                self._calendar_callback_url,
                self._calendar_channel_token_provider is not None,
                self._id_generator is not None,
            )
        )
        if not gmail_configured and not calendar_configured:
            return CommandResult(
                status=CommandStatus.NO_OP,
                identifiers={"maintenance": MaintenanceKind.RENEW_WATCHES.value},
                revision=None,
                error_code="gmail_watch_renewal_not_configured",
            )
        watch = None
        calendar_channel_id = ""
        try:
            if gmail_configured:
                assert self._gmail_reader is not None
                watch = await self._gmail_reader.create_watch(
                    self._controlled_user_id,
                    self._gmail_pubsub_topic,
                    GMAIL_WATCH_LABELS,
                )
            if calendar_configured:
                calendar_channel_id = await self._renew_calendar_watch()
        except SourceAuthorizationError:
            await self._record_request_fields(
                {"status": "reauth_required", "auth_error": "invalid_grant"}
            )
            return CommandResult(
                status=CommandStatus.TERMINAL_FAILURE,
                identifiers={"maintenance": MaintenanceKind.RENEW_WATCHES.value},
                revision=None,
                error_code="reauth_required",
            )
        if watch is not None:
            await self._record_request_fields(
                {
                    "watch_expiration": watch.expiration,
                    "watch_renewed_at": self._clock.now(),
                }
            )
        return CommandResult(
            status=CommandStatus.COMPLETED,
            identifiers={
                "maintenance": MaintenanceKind.RENEW_WATCHES.value,
                "watch_expiration": (
                    watch.expiration.isoformat() if watch is not None else ""
                ),
                "calendar_channel_id": calendar_channel_id,
            },
            revision=None,
            error_code=None,
        )

    async def _renew_calendar_watch(self) -> str:
        assert self._calendar_reader is not None
        assert self._calendar_channel_token_provider is not None
        assert self._id_generator is not None
        now = self._clock.now()
        channel_id = self._id_generator.new_id("calendar-channel")
        token = self._calendar_channel_token_provider().strip()
        if not token:
            raise ValueError("Calendar channel token cannot be empty")
        watch = await self._calendar_reader.create_events_watch(
            self._calendar_id,
            self._calendar_callback_url,
            channel_id,
            token,
            now + timedelta(days=6),
        )

        async def _save(repositories: RepositorySet) -> Mapping[str, Any] | None:
            previous = await repositories.calendar_channels.get(
                self._controlled_user_id
            )
            await repositories.calendar_channels.save(
                self._controlled_user_id,
                {
                    "calendar_id": self._calendar_id,
                    "channel_id": watch.channel_id,
                    "resource_id": watch.resource_id,
                    "resource_uri": watch.resource_uri,
                    "expiration": watch.expiration,
                    "status": "active",
                    "token_hash": watch.token_hash,
                    "registered_at": now,
                    "previous_channel_id": (
                        previous.get("channel_id") if previous else None
                    ),
                    "previous_resource_id": (
                        previous.get("resource_id") if previous else None
                    ),
                    "previous_expiration": (
                        previous.get("expiration") if previous else None
                    ),
                    "previous_status": "overlap" if previous else None,
                    "renewed_at": now,
                },
            )
            return previous

        previous = await self._unit_of_work.run(_save)
        if previous and previous.get("channel_id") and previous.get("resource_id"):
            try:
                await self._calendar_reader.stop_watch(
                    str(previous["channel_id"]), str(previous["resource_id"])
                )
            except Exception:  # noqa: BLE001 - overlap remains verifier-safe until expiry
                pass
        return watch.channel_id

    async def recover_cursors(self, trace_id: str) -> CommandResult:
        """Gmail quiet-window catch-up: if a published cursor exists but no
        publication happened recently and no request is pending, create a
        coalesced sync request and dispatch the bootstrap task."""
        now = self._clock.now()
        request_id = f"{SourceType.GMAIL.value}:{self._controlled_user_id}"

        async def _check(repositories: RepositorySet) -> str | None:
            cursor = await repositories.sync_cursors.get(
                self._controlled_user_id, SourceType.GMAIL
            )
            if cursor is None or cursor.published_cursor is None:
                return "no_published_cursor"
            request = await repositories.sync_requests.get(request_id)
            if request is not None:
                if request.get("status") == "pending":
                    return "already_pending"
                if request.get("status") == "reauth_required":
                    return "reauth_required"
            last_activity = cursor.updated_at
            if now - last_activity < CATCH_UP_QUIET_WINDOW:
                return "recent_activity"
            await repositories.sync_requests.upsert(
                request_id,
                {
                    "source": SourceType.GMAIL.value,
                    "user_id": self._controlled_user_id,
                    "status": "pending",
                    "catch_up": True,
                    "updated_at": now,
                },
            )
            return None

        skip_reason = await self._unit_of_work.run(_check)
        if skip_reason is not None:
            return CommandResult(
                status=CommandStatus.NO_OP,
                identifiers={"maintenance": MaintenanceKind.RECOVER_CURSORS.value},
                revision=None,
                error_code=skip_reason,
            )
        result = await self._source_sync_dispatcher.dispatch(request_id)
        return CommandResult(
            status=CommandStatus.COMPLETED,
            identifiers={
                "maintenance": MaintenanceKind.RECOVER_CURSORS.value,
                "task_name": result.task_name if result else "",
            },
            revision=None,
            error_code=None,
        )

    async def dispatch_pending(self, trace_id: str) -> CommandResult:
        observation_summary = await self._observation_dispatcher.dispatch_pending(
            self._controlled_user_id, self._scan_limit
        )
        outbox_summary = await self._outbox_dispatcher.dispatch_pending(
            self._controlled_user_id, self._scan_limit
        )
        sync_summary = await self._source_sync_dispatcher.dispatch_pending(
            self._scan_limit
        )
        return CommandResult(
            status=CommandStatus.COMPLETED,
            identifiers={
                "maintenance": MaintenanceKind.DISPATCH_PENDING.value,
                "observations_scanned": str(observation_summary.scanned),
                "observations_queued": str(observation_summary.queued),
                "outbox_scanned": str(outbox_summary.scanned),
                "outbox_queued": str(outbox_summary.queued),
                "sync_requests_redispatched": str(sync_summary.queued),
            },
            revision=None,
            error_code=None,
        )

    async def create_safety_observations(self, trace_id: str) -> CommandResult:
        """Persist bounded, deterministic repair inputs from current truth.

        The scheduler never applies lifecycle or planning decisions itself.
        It only records facts which the normal fenced reconciliation path
        consumes. Repeated scans before delivery therefore converge on the
        same observation IDs.
        """
        now = self._clock.now()

        async def _scan(
            repositories: RepositorySet,
        ) -> tuple[list[str], list[str], int, int]:
            commitments = sorted(
                await repositories.commitments.list_active(self._controlled_user_id),
                key=lambda item: item.commitment_id,
            )
            calendar_cursor = await repositories.sync_cursors.get(
                self._controlled_user_id, SourceType.CALENDAR
            )
            calendar_truth_eligible = (
                calendar_cursor is not None
                and calendar_cursor.publish_in_progress_generation_id is None
            )
            candidates: list[_SafetyCandidate] = []
            drift_detected = False
            for commitment in commitments:
                blocks = sorted(
                    await repositories.work_blocks.list_for_commitment(
                        commitment.commitment_id
                    ),
                    key=lambda item: (item.scheduled_start, item.work_block_id),
                )
                for block in blocks:
                    target: WorkBlockExecutionState | None = None
                    if (
                        block.execution_state == WorkBlockExecutionState.PLANNED
                        and now >= block.scheduled_end
                    ):
                        target = WorkBlockExecutionState.AWAITING_CHECK_IN
                    elif (
                        block.execution_state == WorkBlockExecutionState.PLANNED
                        and block.scheduled_start <= now < block.scheduled_end
                    ):
                        target = WorkBlockExecutionState.ACTIVE
                    elif (
                        block.execution_state == WorkBlockExecutionState.ACTIVE
                        and now >= block.scheduled_end
                    ):
                        target = WorkBlockExecutionState.AWAITING_CHECK_IN
                    if target is not None:
                        candidates.append(
                            _SafetyCandidate(
                                producer_id=f"work-block:{block.work_block_id}",
                                producer_version=(
                                    f"revision:{block.revision}:target:{target.value}"
                                ),
                                metadata={
                                    "kind": "work_block_lifecycle",
                                    "work_block_id": block.work_block_id,
                                    "commitment_id": block.commitment_id,
                                    "expected_work_block_revision": block.revision,
                                    "target_execution_state": target.value,
                                    "scheduled_start": block.scheduled_start.isoformat(),
                                    "scheduled_end": block.scheduled_end.isoformat(),
                                },
                            )
                        )
                    if (
                        calendar_truth_eligible
                        and block.execution_state == WorkBlockExecutionState.PLANNED
                        and block.user_edit_state == UserEditState.NONE
                    ):
                        snapshot = await repositories.calendar_snapshots.get(
                            block.calendar_id, block.calendar_event_id
                        )
                        timing_mismatch = (
                            snapshot is not None
                            and (
                                snapshot.is_tombstone
                                or snapshot.observed_start != block.scheduled_start
                                or snapshot.observed_end != block.scheduled_end
                            )
                        )
                        if timing_mismatch and snapshot is not None:
                            drift_detected = True
                            candidates.append(
                                _SafetyCandidate(
                                    producer_id=f"calendar-drift:{block.work_block_id}",
                                    producer_version=CanonicalEncoder.hash(
                                        [
                                            "calendar-drift:v1",
                                            block.revision,
                                            snapshot.observed_payload_hash,
                                            int(snapshot.is_tombstone),
                                        ]
                                    ),
                                    metadata={
                                        "kind": "calendar_drift_detected",
                                        "work_block_id": block.work_block_id,
                                        "commitment_id": block.commitment_id,
                                        "calendar_event_id": block.calendar_event_id,
                                        "desired_start": block.scheduled_start.isoformat(),
                                        "desired_end": block.scheduled_end.isoformat(),
                                        "observed_start": (
                                            snapshot.observed_start.isoformat()
                                            if snapshot.observed_start
                                            else ""
                                        ),
                                        "observed_end": (
                                            snapshot.observed_end.isoformat()
                                            if snapshot.observed_end
                                            else ""
                                        ),
                                        "snapshot_tombstone": snapshot.is_tombstone,
                                        "calendar_state_revision": (
                                            snapshot.calendar_state_revision
                                        ),
                                    },
                                )
                            )

                projection = commitment.projection
                projection_stale = (
                    projection is None
                    or projection.source_commitment_revision != commitment.revision
                    or projection.source_work_block_revision_hash
                    != self._progress.work_block_revision_hash(blocks)
                )
                overdue_refresh = commitment.deadline.value <= now and (
                    projection is None
                    or projection.risk_level != RiskLevel.OVERDUE
                )
                if projection_stale or overdue_refresh:
                    version = CanonicalEncoder.hash(
                        [
                            "periodic-projection-refresh:v1",
                            commitment.commitment_id,
                            commitment.revision,
                            *(f"{block.work_block_id}:{block.revision}" for block in blocks),
                        ]
                    )
                    candidates.append(
                        _SafetyCandidate(
                            producer_id=f"commitment:{commitment.commitment_id}",
                            producer_version=version,
                            metadata={
                                "kind": "portfolio_refresh",
                                "reason": (
                                    "overdue_deadline"
                                    if commitment.deadline.value <= now
                                    else "projection_stale"
                                ),
                                "commitment_id": commitment.commitment_id,
                                "deadline": commitment.deadline.value.isoformat(),
                                "previous_risk": (
                                    projection.risk_level.value if projection else "unknown"
                                ),
                            },
                        )
                    )

            stale_cutoff = now - STUCK_GENERATION_WINDOW
            calculated_runs = await repositories.planner_runs.list_for_user(
                self._controlled_user_id,
                PlannerRunStatus.CALCULATED.value,
                self._scan_limit,
            )
            for plan in calculated_runs:
                if plan.calculated_at > stale_cutoff:
                    continue
                candidates.append(
                    _SafetyCandidate(
                        producer_id=f"planner-run:{plan.planner_run_id}",
                        producer_version=f"status:{plan.status.value}",
                        metadata={
                            "kind": "stale_planner_cleanup",
                            "planner_run_id": plan.planner_run_id,
                            "calculated_at": plan.calculated_at.isoformat(),
                        },
                    )
                )

            created_ids: list[str] = []
            for candidate in candidates[: self._scan_limit]:
                observation = self._observation_factory.continuation(
                    observation_type=ObservationType.PERIODIC_SAFETY,
                    user_id=self._controlled_user_id,
                    producer_id=candidate.producer_id,
                    producer_version=candidate.producer_version,
                    safe_metadata=candidate.metadata,
                    observed_at=now,
                    trace_id=trace_id,
                )
                if await repositories.observations.create(observation):
                    created_ids.append(observation.observation_id)

            delayed_request_ids: list[str] = []
            if drift_detected:
                drift_request_id = (
                    f"{SourceType.CALENDAR.value}:{self._controlled_user_id}"
                )
                existing_drift_request = await repositories.sync_requests.get(
                    drift_request_id
                )
                if not existing_drift_request or existing_drift_request.get("status") != "pending":
                    await repositories.sync_requests.upsert(
                        drift_request_id,
                        {
                            "source": SourceType.CALENDAR.value,
                            "user_id": self._controlled_user_id,
                            "status": "pending",
                            "reason": "desired_snapshot_drift",
                            "sync_generation_id": (
                                "safety-drift-"
                                f"{calendar_cursor.calendar_state_revision if calendar_cursor else 0}"
                            ),
                            "page_sequence": 0,
                            "updated_at": now,
                        },
                    )
                delayed_request_ids.append(drift_request_id)
            for source in (SourceType.GMAIL, SourceType.CALENDAR):
                generation = await repositories.sync_generations.get_active(
                    self._controlled_user_id, source
                )
                if generation is None or now - generation.updated_at < STUCK_GENERATION_WINDOW:
                    continue
                await repositories.sync_requests.upsert(
                    generation.sync_request_id,
                    {
                        "source": source.value,
                        "user_id": self._controlled_user_id,
                        "status": "pending",
                        "sync_generation_id": generation.sync_generation_id,
                        "page_sequence": generation.current_page_sequence + 1,
                        "failure_state": "source_sync_delayed",
                        "delayed_since": generation.updated_at,
                        "updated_at": now,
                    },
                )
                delayed_request_ids.append(generation.sync_request_id)
            return (
                created_ids,
                delayed_request_ids,
                len(candidates),
                len(commitments),
            )

        try:
            created_ids, delayed_request_ids, candidate_count, commitment_count = (
                await self._unit_of_work.run(_scan)
            )
        except ValueError as error:
            # ValueError messages in this boundary are limited to enum/schema and
            # invariant names. Keep the reason server-side for visible operations
            # without returning Firestore details through the HTTP response.
            locations: list[str] = []
            traceback_cursor = error.__traceback__
            while traceback_cursor is not None:
                frame = traceback_cursor.tb_frame
                locations.append(f"{frame.f_code.co_name}:{traceback_cursor.tb_lineno}")
                traceback_cursor = traceback_cursor.tb_next
            logger.exception(
                "safety reconciliation value error",
                extra={
                    "safe_payload": {
                        "reason": str(error),
                        "location": " > ".join(locations[-6:]),
                    }
                },
            )
            raise
        queued = 0
        for observation_id in created_ids:
            try:
                if await self._observation_dispatcher.dispatch(observation_id) is not None:
                    queued += 1
            except Exception:  # noqa: BLE001 - dispatch_pending closes this gap
                pass
        recovered = 0
        for request_id in delayed_request_ids:
            try:
                if await self._source_sync_dispatcher.dispatch(request_id) is not None:
                    recovered += 1
            except Exception:  # noqa: BLE001 - dispatch_pending closes this gap
                pass
        return CommandResult(
            status=(CommandStatus.COMPLETED if created_ids or delayed_request_ids else CommandStatus.NO_OP),
            identifiers={
                "maintenance": MaintenanceKind.SAFETY_RECONCILIATION.value,
                "active_commitments_scanned": str(commitment_count),
                "safety_candidates": str(candidate_count),
                "observations_created": str(len(created_ids)),
                "observations_queued": str(queued),
                "delayed_generations_recovered": str(recovered),
            },
            revision=None,
            error_code=None,
        )

    async def _record_request_fields(self, fields: Mapping[str, Any]) -> None:
        request_id = f"{SourceType.GMAIL.value}:{self._controlled_user_id}"
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            await repositories.sync_requests.upsert(
                request_id,
                {
                    "source": SourceType.GMAIL.value,
                    "user_id": self._controlled_user_id,
                    "updated_at": now,
                    **fields,
                },
            )

        await self._unit_of_work.run(_record)
