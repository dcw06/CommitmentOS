from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from commitmentos.application.dto import FencedLease
from commitmentos.contracts.auth import OAuthTransaction, OAuthTransactionStatus
from commitmentos.contracts.observations import (
    TERMINAL_RECONCILIATION_STATUSES,
    ObservationV1,
    ReconciliationStatus,
)
from commitmentos.contracts.synchronization import (
    SyncApplyCheckpoint,
    SyncCursor,
    SyncGeneration,
    SyncGenerationItem,
    SyncGenerationItemStatus,
    SyncGenerationStatus,
    SyncManifestHash,
    SyncPageCheckpoint,
    SyncPublicationBarrier,
)
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.actions.models import ActionOutbox
from commitmentos.domain.audit.models import ActivityEvent
from commitmentos.domain.commitments.models import Commitment, LifecycleStatus
from commitmentos.domain.controls.models import SystemControls, initial_system_controls
from commitmentos.domain.planning.calendar_state import (
    CalendarEventSnapshot,
    CalendarSnapshotReducer,
    CalendarStateSnapshot,
)
from commitmentos.domain.planning.models import PortfolioPlan
from commitmentos.domain.progress.models import WorkBlock
from commitmentos.domain.shared.errors import (
    InvalidTransitionError,
    RevisionConflictError,
)
from commitmentos.domain.shared.types import CanonicalEncoder
from commitmentos.infrastructure.firestore.serializers import (
    SerializerRegistry,
    _require_utc,
)
from commitmentos.infrastructure.firestore.unit_of_work import FirestoreTransactionContext

USERS = "users"
COMMITMENTS = "commitments"
WORK_BLOCKS = "work_blocks"
SOURCE_OBSERVATIONS = "source_observations"
EVIDENCE = "evidence"
SOURCE_SPAN_DISMISSALS = "source_span_dismissals"
APPROVALS = "approvals"
ACTION_OUTBOX = "action_outbox"
ACTIVITY_EVENTS = "activity_events"
SYNC_REQUESTS = "sync_requests"
SYNC_CURSORS = "sync_cursors"
SYNC_GENERATIONS = "sync_generations"
SYNC_GENERATION_ITEMS = "sync_generation_items"
PROCESSING_LEASES = "processing_leases"
WEB_SESSIONS = "web_sessions"
OAUTH_TRANSACTIONS = "oauth_transactions"
PLANNER_RUNS = "planner_runs"
RECONCILIATION_RUNS = "reconciliation_runs"
SYSTEM_CONTROLS = "system_controls"
CALENDAR_EVENT_SNAPSHOTS = "calendar_event_snapshots"
CALENDAR_CHANNELS = "calendar_channels"
CALENDAR_CHANNEL_RATE_LIMITS = "calendar_channel_rate_limits"
LEGACY_CURSOR_UNKNOWN_UPDATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)

ACTIVE_LIFECYCLE_STATUSES = [
    LifecycleStatus.ACTIVE.value,
    LifecycleStatus.IN_PROGRESS.value,
    LifecycleStatus.COMPLETION_CANDIDATE.value,
]


class FirestoreUserRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, user_id: str) -> Mapping[str, Any] | None:
        return await self._context.get(USERS, user_id)

    async def save(self, user_id: str, user: Mapping[str, Any]) -> None:
        self._context.stage_set(USERS, user_id, dict(user))


class FirestoreCommitmentRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.commitments

    async def get(self, commitment_id: str) -> Commitment | None:
        document = await self._context.get(COMMITMENTS, commitment_id)
        if document is None:
            return None
        return self._serializer.from_document(commitment_id, document)

    async def list_active(self, user_id: str) -> Sequence[Commitment]:
        rows = await self._context.query(
            COMMITMENTS,
            [
                ("user_id", "==", user_id),
                ("lifecycle_status", "in", ACTIVE_LIFECYCLE_STATUSES),
            ],
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_thread(self, user_id: str, thread_id: str) -> Sequence[Commitment]:
        rows = await self._context.query(
            COMMITMENTS,
            [
                ("user_id", "==", user_id),
                ("source_thread_id", "==", thread_id),
            ],
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_user(
        self,
        user_id: str,
        lifecycle_status: LifecycleStatus | None,
        before: datetime | None,
        limit: int,
    ) -> Sequence[Commitment]:
        filters: list[tuple[str, str, Any]] = [("user_id", "==", user_id)]
        if lifecycle_status is not None:
            filters.append(("lifecycle_status", "==", lifecycle_status.value))
        if before is not None:
            filters.append(("updated_at", "<", before))
        rows = await self._context.query(
            COMMITMENTS,
            filters,
            order_by=("updated_at", "DESCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def save(self, commitment: Commitment, expected_revision: int | None) -> None:
        current = await self._context.get(COMMITMENTS, commitment.commitment_id)
        if expected_revision is None:
            if current is not None:
                raise RevisionConflictError(
                    f"commitment {commitment.commitment_id} already exists"
                )
            self._context.stage_create(
                COMMITMENTS,
                commitment.commitment_id,
                dict(self._serializer.to_document(commitment)),
            )
            return
        if current is None:
            raise RevisionConflictError(f"commitment {commitment.commitment_id} does not exist")
        if current["revision"] != expected_revision:
            raise RevisionConflictError(
                f"commitment {commitment.commitment_id} revision is {current['revision']}, "
                f"expected {expected_revision}"
            )
        self._context.stage_set(
            COMMITMENTS,
            commitment.commitment_id,
            dict(self._serializer.to_document(commitment)),
        )


class FirestoreWorkBlockRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.work_blocks

    async def get(self, work_block_id: str) -> WorkBlock | None:
        document = await self._context.get(WORK_BLOCKS, work_block_id)
        if document is None:
            return None
        return self._serializer.from_document(work_block_id, document)

    async def list_for_commitment(self, commitment_id: str) -> Sequence[WorkBlock]:
        rows = await self._context.query(
            WORK_BLOCKS,
            [("commitment_id", "==", commitment_id)],
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_user_horizon(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[WorkBlock]:
        commitment_rows = await self._context.query(
            COMMITMENTS,
            [("user_id", "==", user_id)],
        )
        work_blocks: list[WorkBlock] = []
        for commitment_id, _ in commitment_rows:
            rows = await self._context.query(
                WORK_BLOCKS,
                [
                    ("commitment_id", "==", commitment_id),
                    ("scheduled_start", "<", end),
                ],
            )
            for work_block_id, document in rows:
                block = self._serializer.from_document(work_block_id, document)
                if block.scheduled_end > start:
                    work_blocks.append(block)
        return sorted(
            work_blocks,
            key=lambda block: (block.scheduled_start, block.work_block_id),
        )

    async def save(self, work_block: WorkBlock, expected_revision: int | None) -> None:
        current = await self._context.get(WORK_BLOCKS, work_block.work_block_id)
        if expected_revision is None:
            if current is not None:
                raise RevisionConflictError(f"work block {work_block.work_block_id} already exists")
            self._context.stage_create(
                WORK_BLOCKS,
                work_block.work_block_id,
                dict(self._serializer.to_document(work_block)),
            )
            return
        if current is None:
            raise RevisionConflictError(f"work block {work_block.work_block_id} does not exist")
        if current["revision"] != expected_revision:
            raise RevisionConflictError(
                f"work block {work_block.work_block_id} revision is {current['revision']}, "
                f"expected {expected_revision}"
            )
        self._context.stage_set(
            WORK_BLOCKS,
            work_block.work_block_id,
            dict(self._serializer.to_document(work_block)),
        )


class FirestoreObservationRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.observations

    async def get(self, observation_id: str) -> ObservationV1 | None:
        document = await self._context.get(SOURCE_OBSERVATIONS, observation_id)
        if document is None:
            return None
        return self._serializer.from_document(observation_id, document)

    async def create(self, observation: ObservationV1) -> bool:
        current = await self._context.get(SOURCE_OBSERVATIONS, observation.observation_id)
        if current is not None:
            return False
        self._context.stage_create(
            SOURCE_OBSERVATIONS,
            observation.observation_id,
            dict(self._serializer.to_document(observation)),
        )
        return True

    async def set_reconciliation_status(
        self,
        observation_id: str,
        expected_statuses: set[ReconciliationStatus],
        target_status: ReconciliationStatus,
        dispatch_generation: int,
        control_epoch: int | None,
        expected_processing_fencing_token: str | None,
        processing_fence: FencedLease | None,
    ) -> ObservationV1:
        document = await self._context.get(SOURCE_OBSERVATIONS, observation_id)
        if document is None:
            raise InvalidTransitionError(f"observation {observation_id} does not exist")
        observation = self._serializer.from_document(observation_id, document)
        if observation.reconciliation_status not in expected_statuses:
            raise InvalidTransitionError(
                f"observation {observation_id} is {observation.reconciliation_status}; "
                f"expected one of {[status.value for status in expected_statuses]}"
            )
        if (
            expected_processing_fencing_token is not None
            and observation.processing_fencing_token != expected_processing_fencing_token
        ):
            raise InvalidTransitionError(
                f"observation {observation_id} fencing token does not match; a newer worker owns it"
            )
        updated = dict(document)
        updated["reconciliation_status"] = target_status.value
        updated["dispatch_generation"] = dispatch_generation
        if target_status == ReconciliationStatus.HELD_BY_CONTROL:
            updated["held_control_epoch"] = control_epoch
        if target_status == ReconciliationStatus.PROCESSING:
            if processing_fence is None:
                raise InvalidTransitionError("processing claim requires a processing fence")
            updated["claimed_control_epoch"] = control_epoch
            updated["processing_lease_key"] = processing_fence.lease_key
            updated["processing_lease_owner"] = processing_fence.owner
            updated["processing_fencing_token"] = processing_fence.fencing_token
            updated["processing_lease_expires_at"] = processing_fence.expires_at
            updated["processing_attempt"] = observation.processing_attempt + 1
        if (
            target_status in TERMINAL_RECONCILIATION_STATUSES
            or target_status == ReconciliationStatus.QUEUED
            or target_status == ReconciliationStatus.RETRYABLE_FAILED
        ):
            updated["processing_lease_key"] = None
            updated["processing_lease_owner"] = None
            updated["processing_lease_expires_at"] = None
            if target_status != ReconciliationStatus.RETRYABLE_FAILED:
                updated["processing_fencing_token"] = None
        self._context.stage_set(SOURCE_OBSERVATIONS, observation_id, updated)
        return self._serializer.from_document(observation_id, updated)

    async def list_pending(self, user_id: str, limit: int) -> Sequence[ObservationV1]:
        # "Pending" means dispatch-eligible: PENDING never dispatched, or
        # QUEUED whose named task may have been lost in the enqueue gap.
        rows = await self._context.query(
            SOURCE_OBSERVATIONS,
            [
                ("user_id", "==", user_id),
                (
                    "reconciliation_status",
                    "in",
                    [ReconciliationStatus.PENDING.value, ReconciliationStatus.QUEUED.value],
                ),
            ],
            order_by=("observed_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_held(self, user_id: str, limit: int) -> Sequence[ObservationV1]:
        rows = await self._context.query(
            SOURCE_OBSERVATIONS,
            [
                ("user_id", "==", user_id),
                ("reconciliation_status", "==", ReconciliationStatus.HELD_BY_CONTROL.value),
            ],
            order_by=("observed_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_staged_for_release(
        self,
        user_id: str,
        limit: int,
    ) -> Sequence[ObservationV1]:
        """Observations materialized by a sync apply phase, not yet released.

        Staged observations are ineligible for every dispatch scan until their
        generation publishes (architecture §11.5 step 7); release flips them to
        `pending` in bounded batches after publication.
        """
        rows = await self._context.query(
            SOURCE_OBSERVATIONS,
            [
                ("user_id", "==", user_id),
                ("reconciliation_status", "==", ReconciliationStatus.STAGED.value),
            ],
            order_by=("observed_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]


class FirestoreEvidenceRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, evidence_id: str) -> Mapping[str, Any] | None:
        return await self._context.get(EVIDENCE, evidence_id)

    async def create(self, evidence: Mapping[str, Any]) -> bool:
        evidence_id = evidence["evidence_id"]
        current = await self._context.get(EVIDENCE, evidence_id)
        if current is not None:
            return False
        self._context.stage_create(EVIDENCE, evidence_id, dict(evidence))
        return True

    async def list_for_commitment(self, commitment_id: str) -> Sequence[Mapping[str, Any]]:
        rows = await self._context.query(
            EVIDENCE,
            [("commitment_id", "==", commitment_id)],
        )
        return [dict(data) | {"evidence_id": doc_id} for doc_id, data in rows]


class FirestoreSourceSpanDismissalRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, dismissal_id: str) -> Mapping[str, Any] | None:
        document = await self._context.get(SOURCE_SPAN_DISMISSALS, dismissal_id)
        if document is None:
            return None
        return dict(document) | {"dismissal_id": dismissal_id}

    async def create(self, dismissal: Mapping[str, Any]) -> bool:
        dismissal_id = str(dismissal["dismissal_id"])
        if await self._context.get(SOURCE_SPAN_DISMISSALS, dismissal_id) is not None:
            return False
        document = {
            key: value for key, value in dismissal.items() if key != "dismissal_id"
        }
        self._context.stage_create(SOURCE_SPAN_DISMISSALS, dismissal_id, document)
        return True

    async def list_for_thread(
        self,
        user_id: str,
        thread_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        rows = await self._context.query(
            SOURCE_SPAN_DISMISSALS,
            [("user_id", "==", user_id), ("thread_id", "==", thread_id)],
        )
        return [dict(data) | {"dismissal_id": doc_id} for doc_id, data in rows]


class FirestoreApprovalRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, approval_id: str) -> Mapping[str, Any] | None:
        document = await self._context.get(APPROVALS, approval_id)
        if document is None:
            return None
        return dict(document) | {"approval_id": approval_id}

    async def create(self, approval: Mapping[str, Any]) -> bool:
        approval_id = approval["approval_id"]
        current = await self._context.get(APPROVALS, approval_id)
        if current is not None:
            return False
        document = {key: value for key, value in approval.items() if key != "approval_id"}
        self._context.stage_create(APPROVALS, approval_id, document)
        return True

    async def resolve(
        self,
        approval_id: str,
        expected_revision: int,
        decision: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        document = await self._context.get(APPROVALS, approval_id)
        if document is None:
            raise InvalidTransitionError(f"approval {approval_id} does not exist")
        if document.get("status") != "pending":
            raise RevisionConflictError(
                f"approval {approval_id} is {document.get('status')}; only one decision can win"
            )
        if document.get("revision") != expected_revision:
            raise RevisionConflictError(
                f"approval {approval_id} revision is {document.get('revision')}, "
                f"expected {expected_revision}"
            )
        updated = dict(document)
        updated["status"] = decision.get("status", "resolved")
        updated["decision"] = dict(decision)
        updated["revision"] = expected_revision + 1
        self._context.stage_set(APPROVALS, approval_id, updated)
        return dict(updated) | {"approval_id": approval_id}

    async def list_pending(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        rows = await self._context.query(
            APPROVALS,
            [
                ("user_id", "==", user_id),
                ("status", "==", "pending"),
            ],
        )
        return [dict(data) | {"approval_id": doc_id} for doc_id, data in rows]


class FirestoreActionOutboxRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.outbox

    async def get(self, outbox_id: str) -> ActionOutbox | None:
        document = await self._context.get(ACTION_OUTBOX, outbox_id)
        if document is None:
            return None
        return self._serializer.from_document(outbox_id, document)

    async def create(self, action: ActionOutbox) -> bool:
        current = await self._context.get(ACTION_OUTBOX, action.outbox_id)
        if current is not None:
            return False
        self._context.stage_create(
            ACTION_OUTBOX,
            action.outbox_id,
            dict(self._serializer.to_document(action)),
        )
        return True

    async def save(self, action: ActionOutbox, expected_updated_at: datetime) -> None:
        current = await self._context.get(ACTION_OUTBOX, action.outbox_id)
        if current is None:
            raise RevisionConflictError(f"outbox {action.outbox_id} does not exist")
        stored = self._serializer.from_document(action.outbox_id, current)
        if stored.updated_at != expected_updated_at:
            raise RevisionConflictError(
                f"outbox {action.outbox_id} changed at {stored.updated_at}, "
                f"expected {expected_updated_at}"
            )
        self._context.stage_set(
            ACTION_OUTBOX,
            action.outbox_id,
            dict(self._serializer.to_document(action)),
        )

    async def list_pending_dispatch(self, user_id: str, limit: int) -> Sequence[ActionOutbox]:
        # Includes QUEUED so the periodic dispatcher can recreate a named task
        # lost in the write-before-enqueue gap; execution-status guards keep
        # the recreated delivery idempotent.
        rows = await self._context.query(
            ACTION_OUTBOX,
            [
                ("user_id", "==", user_id),
                ("dispatch_status", "in", ["pending", "queued"]),
            ],
            order_by=("created_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_held(self, user_id: str, limit: int) -> Sequence[ActionOutbox]:
        rows = await self._context.query(
            ACTION_OUTBOX,
            [
                ("user_id", "==", user_id),
                ("execution_status", "==", "held_by_control"),
            ],
            order_by=("created_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_work_block(
        self,
        user_id: str,
        work_block_id: str,
        limit: int,
    ) -> Sequence[ActionOutbox]:
        rows = await self._context.query(
            ACTION_OUTBOX,
            [("user_id", "==", user_id), ("work_block_id", "==", work_block_id)],
            order_by=("created_at", "DESCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_user_statuses(
        self,
        user_id: str,
        execution_statuses: Sequence[str],
        limit: int,
    ) -> Sequence[ActionOutbox]:
        if not execution_statuses:
            return []
        rows = await self._context.query(
            ACTION_OUTBOX,
            [
                ("user_id", "==", user_id),
                ("execution_status", "in", list(execution_statuses)),
            ],
            order_by=("created_at", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]


class FirestoreActivityRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.activity

    async def append(self, event: ActivityEvent) -> None:
        current = await self._context.get(ACTIVITY_EVENTS, event.activity_event_id)
        if current is not None:
            # Deterministic IDs make replayed transactions converge on one event.
            return
        self._context.stage_create(
            ACTIVITY_EVENTS,
            event.activity_event_id,
            dict(self._serializer.to_document(event)),
        )

    async def list_for_user(
        self,
        user_id: str,
        before: datetime | None,
        limit: int,
    ) -> Sequence[ActivityEvent]:
        filters: list[tuple[str, str, Any]] = [("user_id", "==", user_id)]
        if before is not None:
            filters.append(("created_at", "<", before))
        rows = await self._context.query(
            ACTIVITY_EVENTS,
            filters,
            order_by=("created_at", "DESCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]


class FirestoreSyncRequestRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, sync_request_id: str) -> Mapping[str, Any] | None:
        document = await self._context.get(SYNC_REQUESTS, sync_request_id)
        if document is None:
            return None
        return dict(document) | {"sync_request_id": sync_request_id}

    async def upsert(self, sync_request_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        current = await self._context.get(SYNC_REQUESTS, sync_request_id)
        if current is None:
            document = dict(request)
            document.setdefault("status", "pending")
            document["signal_count"] = 1
            self._context.stage_create(SYNC_REQUESTS, sync_request_id, document)
        else:
            document = dict(current)
            document.update(request)
            document["signal_count"] = int(current.get("signal_count", 0)) + 1
            self._context.stage_set(SYNC_REQUESTS, sync_request_id, document)
        return dict(document) | {"sync_request_id": sync_request_id}

    async def list_pending(self, limit: int) -> Sequence[Mapping[str, Any]]:
        rows = await self._context.query(
            SYNC_REQUESTS,
            [("status", "==", "pending")],
            limit=limit,
        )
        return [dict(data) | {"sync_request_id": doc_id} for doc_id, data in rows]

    async def list_for_user(
        self, user_id: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        rows = await self._context.query(
            SYNC_REQUESTS,
            [("user_id", "==", user_id)],
            order_by=("updated_at", "DESCENDING"),
            limit=limit,
        )
        return [dict(data) | {"sync_request_id": doc_id} for doc_id, data in rows]


class FirestoreProcessingLeaseRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def acquire(
        self,
        lease_key: str,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> FencedLease | None:
        document = await self._context.get(PROCESSING_LEASES, lease_key)
        if document is not None:
            current_expiry = document.get("expires_at")
            held_by_other = document.get("owner") != owner
            unexpired = current_expiry is not None and current_expiry > now
            if held_by_other and unexpired:
                return None
        counter = int(document.get("fencing_counter", 0)) + 1 if document else 1
        fencing_token = str(counter)
        self._context.stage_set(
            PROCESSING_LEASES,
            lease_key,
            {
                "owner": owner,
                "fencing_counter": counter,
                "fencing_token": fencing_token,
                "expires_at": expires_at,
            },
        )
        return FencedLease(
            lease_key=lease_key,
            owner=owner,
            fencing_token=fencing_token,
            expires_at=expires_at,
        )

    async def release(self, lease_key: str, owner: str, fencing_token: str) -> None:
        document = await self._context.get(PROCESSING_LEASES, lease_key)
        if document is None:
            return
        if document.get("owner") != owner or document.get("fencing_token") != fencing_token:
            # A newer worker owns the lease; releasing would break its fence.
            return
        self._context.stage_delete(PROCESSING_LEASES, lease_key)

    async def verify(self, fence: FencedLease, at: datetime) -> None:
        document = await self._context.get(PROCESSING_LEASES, fence.lease_key)
        if document is None:
            raise InvalidTransitionError(f"lease {fence.lease_key} no longer exists")
        if (
            document.get("owner") != fence.owner
            or document.get("fencing_token") != fence.fencing_token
        ):
            raise InvalidTransitionError(
                f"lease {fence.lease_key} was taken over by a newer worker"
            )
        expires_at = document.get("expires_at")
        if expires_at is not None and expires_at < at:
            raise InvalidTransitionError(f"lease {fence.lease_key} expired at {expires_at}")


class FirestoreWebSessionRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get_by_hash(self, session_id_hash: str) -> Mapping[str, Any] | None:
        return await self._context.get(WEB_SESSIONS, session_id_hash)

    async def create(self, session: Mapping[str, Any]) -> None:
        session_id_hash = session["session_id_hash"]
        document = {key: value for key, value in session.items() if key != "session_id_hash"}
        self._context.stage_create(WEB_SESSIONS, session_id_hash, document)

    async def revoke(self, session_id_hash: str, revoked_at: datetime) -> None:
        document = await self._context.get(WEB_SESSIONS, session_id_hash)
        if document is None:
            return
        updated = dict(document)
        updated["revoked_at"] = revoked_at
        self._context.stage_set(WEB_SESSIONS, session_id_hash, updated)


class FirestoreOAuthTransactionRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.oauth_transactions

    async def create(self, transaction: OAuthTransaction) -> bool:
        current = await self._context.get(OAUTH_TRANSACTIONS, transaction.state_hash)
        if current is not None:
            return False
        self._context.stage_create(
            OAUTH_TRANSACTIONS,
            transaction.state_hash,
            dict(self._serializer.to_document(transaction)),
        )
        return True

    async def consume_pending(
        self,
        state_hash: str,
        consumed_at: datetime,
    ) -> OAuthTransaction:
        document = await self._context.get(OAUTH_TRANSACTIONS, state_hash)
        if document is None:
            raise InvalidTransitionError("oauth transaction does not exist")
        transaction = self._serializer.from_document(state_hash, document)
        if transaction.status != OAuthTransactionStatus.PENDING:
            raise RevisionConflictError("oauth transaction was already consumed")
        if transaction.expires_at < consumed_at:
            raise InvalidTransitionError("oauth transaction expired")
        updated = dict(document)
        updated["status"] = OAuthTransactionStatus.CONSUMED.value
        updated["consumed_at"] = consumed_at
        updated["revision"] = transaction.revision + 1
        self._context.stage_set(OAUTH_TRANSACTIONS, state_hash, updated)
        return self._serializer.from_document(state_hash, updated)

    async def delete_expired(self, expired_before: datetime, limit: int) -> int:
        rows = await self._context.query(
            OAUTH_TRANSACTIONS,
            [("expires_at", "<", expired_before)],
            limit=limit,
        )
        for doc_id, _ in rows:
            self._context.stage_delete(OAUTH_TRANSACTIONS, doc_id)
        return len(rows)


class FirestorePlannerRunRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.portfolio_plans

    async def get(self, planner_run_id: str) -> PortfolioPlan | None:
        document = await self._context.get(PLANNER_RUNS, planner_run_id)
        if document is None:
            return None
        return self._serializer.from_document(planner_run_id, document)

    async def create(self, plan: PortfolioPlan) -> bool:
        current = await self._context.get(PLANNER_RUNS, plan.planner_run_id)
        if current is not None:
            return False
        self._context.stage_create(
            PLANNER_RUNS,
            plan.planner_run_id,
            dict(self._serializer.to_document(plan)),
        )
        return True

    async def save(self, plan: PortfolioPlan, expected_status: str) -> None:
        current = await self._context.get(PLANNER_RUNS, plan.planner_run_id)
        if current is None:
            raise RevisionConflictError(
                f"planner run {plan.planner_run_id} does not exist"
            )
        if current.get("status") != expected_status:
            raise RevisionConflictError(
                f"planner run {plan.planner_run_id} status is {current.get('status')}; "
                f"expected {expected_status}"
            )
        self._context.stage_set(
            PLANNER_RUNS,
            plan.planner_run_id,
            dict(self._serializer.to_document(plan)),
        )

    async def list_for_user(
        self,
        user_id: str,
        status: str | None,
        limit: int,
    ) -> Sequence[PortfolioPlan]:
        filters: list[tuple[str, str, Any]] = [("user_id", "==", user_id)]
        if status is not None:
            filters.append(("status", "==", status))
        rows = await self._context.query(
            PLANNER_RUNS,
            filters,
            order_by=("calculated_at", "DESCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]


class FirestoreReconciliationRunRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, run_id: str) -> Mapping[str, Any] | None:
        document = await self._context.get(RECONCILIATION_RUNS, run_id)
        if document is None:
            return None
        return dict(document) | {"run_id": run_id}

    async def save(
        self,
        run_id: str,
        run: Mapping[str, Any],
        expected_processing_fencing_token: str,
    ) -> None:
        # The authoritative fencing check happens on the observation-status
        # transition committed in this same transaction; the run document
        # records which fencing token produced the outcome.
        document = {key: value for key, value in run.items() if key != "run_id"}
        document["processing_fencing_token"] = expected_processing_fencing_token
        self._context.stage_set(RECONCILIATION_RUNS, run_id, document)


class FirestoreSystemControlRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.system_controls

    async def get(self, user_id: str) -> SystemControls:
        document = await self._context.get(SYSTEM_CONTROLS, user_id)
        if document is None:
            return initial_system_controls(user_id, datetime.now(timezone.utc))
        return self._serializer.from_document(user_id, document)

    async def save(self, controls: SystemControls, expected_epoch: int) -> None:
        document = await self._context.get(SYSTEM_CONTROLS, controls.user_id)
        if document is None:
            if expected_epoch != 1:
                raise RevisionConflictError(
                    "system controls do not exist; expected the bootstrap epoch"
                )
            self._context.stage_create(
                SYSTEM_CONTROLS,
                controls.user_id,
                dict(self._serializer.to_document(controls)),
            )
            return
        if document["control_epoch"] != expected_epoch:
            raise RevisionConflictError(
                f"control epoch is {document['control_epoch']}, expected {expected_epoch}"
            )
        self._context.stage_set(
            SYSTEM_CONTROLS,
            controls.user_id,
            dict(self._serializer.to_document(controls)),
        )


class FirestoreCalendarSnapshotRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializers = serializers
        self._serializer = serializers.calendar_snapshots

    @staticmethod
    def snapshot_id(calendar_id: str, calendar_event_id: str) -> str:
        return CanonicalEncoder.hash(["calendar-snapshot:v1", calendar_id, calendar_event_id])

    async def get(self, calendar_id: str, calendar_event_id: str) -> CalendarEventSnapshot | None:
        document_id = self.snapshot_id(calendar_id, calendar_event_id)
        document = await self._context.get(CALENDAR_EVENT_SNAPSHOTS, document_id)
        if document is None:
            return None
        return self._serializer.from_document(document_id, document)

    async def save(self, snapshot: CalendarEventSnapshot) -> None:
        self._context.stage_set(
            CALENDAR_EVENT_SNAPSHOTS,
            snapshot.calendar_snapshot_id,
            dict(self._serializer.to_document(snapshot)),
        )

    async def list_for_calendar(
        self,
        user_id: str,
        calendar_id: str,
        after_snapshot_id: str | None,
        limit: int,
    ) -> Sequence[CalendarEventSnapshot]:
        filters: list[tuple[str, str, Any]] = [
            ("user_id", "==", user_id),
            ("calendar_id", "==", calendar_id),
        ]
        if after_snapshot_id is not None:
            filters.append(("calendar_snapshot_id", ">", after_snapshot_id))
        rows = await self._context.query(
            CALENDAR_EVENT_SNAPSHOTS,
            filters,
            order_by=("calendar_snapshot_id", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def load_consistent_state(
        self,
        user_id: str,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> CalendarStateSnapshot:
        cursor_repository = FirestoreSyncCursorRepository(
            self._context,
            self._serializers,
        )
        cursor = await cursor_repository.get(user_id, SourceType.CALENDAR)
        if cursor is None or cursor.published_generation_id is None:
            raise InvalidTransitionError("calendar snapshot has not been published")
        if cursor.publish_in_progress_generation_id is not None:
            raise InvalidTransitionError("calendar publication is in progress")
        if cursor.full_resync_required:
            raise InvalidTransitionError("calendar full resynchronization is required")
        rows = await self._context.query(
            CALENDAR_EVENT_SNAPSHOTS,
            [("user_id", "==", user_id), ("calendar_id", "==", calendar_id)],
            order_by=("calendar_snapshot_id", "ASCENDING"),
        )
        events = tuple(
            self._serializer.from_document(document_id, document)
            for document_id, document in rows
        )
        reducer = CalendarSnapshotReducer()
        return CalendarStateSnapshot(
            calendar_id=calendar_id,
            calendar_state_revision=cursor.calendar_state_revision,
            calendar_snapshot_hash=reducer.snapshot_hash(events),
            events=events,
        )


ACTIVE_GENERATION_STATUSES = [
    SyncGenerationStatus.STAGING.value,
    SyncGenerationStatus.APPLYING.value,
    SyncGenerationStatus.READY_TO_PUBLISH.value,
]


class FirestoreCalendarChannelRepository:
    def __init__(self, context: FirestoreTransactionContext) -> None:
        self._context = context

    async def get(self, user_id: str) -> Mapping[str, Any] | None:
        document = await self._context.get(CALENDAR_CHANNELS, user_id)
        if document is None:
            return None
        return dict(document) | {"user_id": user_id}

    async def get_by_channel_id(self, channel_id: str) -> Mapping[str, Any] | None:
        current = await self._context.query(
            CALENDAR_CHANNELS, [("channel_id", "==", channel_id)], limit=1
        )
        if current:
            document_id, document = current[0]
            return dict(document) | {"user_id": document.get("user_id", document_id)}
        previous = await self._context.query(
            CALENDAR_CHANNELS,
            [("previous_channel_id", "==", channel_id)],
            limit=1,
        )
        if not previous:
            return None
        document_id, document = previous[0]
        return dict(document) | {"user_id": document.get("user_id", document_id)}

    async def save(self, user_id: str, channel: Mapping[str, Any]) -> None:
        self._context.stage_set(
            CALENDAR_CHANNELS, user_id, dict(channel) | {"user_id": user_id}
        )

    async def consume_rate_limit(
        self,
        channel_id: str,
        now: datetime,
        window_seconds: int,
        maximum_signals: int,
    ) -> bool:
        document = await self._context.get(CALENDAR_CHANNEL_RATE_LIMITS, channel_id)
        cutoff = now.timestamp() - window_seconds
        signal_times = [
            float(value)
            for value in (document or {}).get("signal_times", [])
            if float(value) >= cutoff
        ]
        if len(signal_times) >= maximum_signals:
            return False
        signal_times.append(now.timestamp())
        self._context.stage_set(
            CALENDAR_CHANNEL_RATE_LIMITS,
            channel_id,
            {"signal_times": signal_times, "updated_at": now},
        )
        return True


def sync_cursor_document_id(user_id: str, source: SourceType) -> str:
    # Matches the spike's `gmail:{user}` document so the live cursor written
    # by watch registration is adopted rather than duplicated.
    return f"{source.value}:{user_id}"


class FirestoreSyncCursorRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.sync_cursors

    async def get(self, user_id: str, source: SourceType) -> SyncCursor | None:
        document_id = sync_cursor_document_id(user_id, source)
        document = await self._context.get(SYNC_CURSORS, document_id)
        if document is None:
            return None
        if "revision" not in document:
            # Spike-era cursor (Phase 0 watch registration wrote only the
            # published history id). Adopt it as revision 0; the first real
            # publication rewrites the document in the Phase 2 shape.
            published = document.get("published_history_id")
            if source == SourceType.CALENDAR:
                published = document.get("published_sync_token", published)
            return SyncCursor(
                user_id=user_id,
                source=source,
                revision=0,
                published_cursor=str(published) if published is not None else None,
                published_generation_id=None,
                publish_in_progress_generation_id=None,
                generation_counter=0,
                calendar_state_revision=0,
                full_resync_required=False,
                # Some Phase 0 Calendar spike cursors predate activity
                # timestamps entirely. Unknown age must be treated as stale,
                # never fresh; the first real publication rewrites the cursor
                # in the canonical versioned shape.
                updated_at=_require_utc(
                    document.get("updated_at")
                    or document.get("watch_registered_at")
                    or document.get("published_at")
                    or LEGACY_CURSOR_UNKNOWN_UPDATED_AT
                ),
            )
        return self._serializer.from_document(document_id, document)

    async def save(self, cursor: SyncCursor) -> None:
        self._context.stage_set(
            SYNC_CURSORS,
            sync_cursor_document_id(cursor.user_id, cursor.source),
            dict(self._serializer.to_document(cursor)),
        )

    async def activate_publication_barrier(
        self,
        barrier: SyncPublicationBarrier,
        expected_cursor_revision: int,
        fence: FencedLease,
    ) -> SyncCursor:
        cursor = await self.get(barrier.user_id, barrier.source)
        if cursor is None:
            raise InvalidTransitionError("cannot activate a barrier without a published cursor")
        if cursor.revision != expected_cursor_revision:
            raise RevisionConflictError(
                f"cursor revision {cursor.revision} != expected {expected_cursor_revision}"
            )
        if cursor.publish_in_progress_generation_id == barrier.sync_generation_id:
            return cursor  # Re-entered apply after a retry: barrier already active.
        if cursor.publish_in_progress_generation_id is not None:
            raise InvalidTransitionError(
                "another generation holds the publication barrier: "
                f"{cursor.publish_in_progress_generation_id}"
            )
        updated = SyncCursor(
            user_id=cursor.user_id,
            source=cursor.source,
            revision=cursor.revision,
            published_cursor=cursor.published_cursor,
            published_generation_id=cursor.published_generation_id,
            publish_in_progress_generation_id=barrier.sync_generation_id,
            generation_counter=cursor.generation_counter,
            calendar_state_revision=cursor.calendar_state_revision,
            full_resync_required=cursor.full_resync_required,
            updated_at=barrier.activated_at,
        )
        await self.save(updated)
        return updated

    async def publish_generation(
        self,
        generation: SyncGeneration,
        expected_generation_status: SyncGenerationStatus,
        expected_staged_manifest: SyncManifestHash,
        expected_applied_manifest: SyncManifestHash,
        fence: FencedLease,
        published_at: datetime,
    ) -> SyncCursor:
        """The single final publication transaction (architecture §11.5 step 6).

        Stages both the cursor promotion and the generation's `published`
        transition so the candidate cursor becomes authoritative exactly once,
        atomically with clearing the barrier.
        """
        current_doc = await self._context.get(SYNC_GENERATIONS, generation.sync_generation_id)
        if current_doc is None:
            raise InvalidTransitionError("generation does not exist")
        if current_doc.get("status") == SyncGenerationStatus.PUBLISHED.value:
            cursor = await self.get(generation.user_id, generation.source)
            if cursor is None or cursor.published_generation_id != generation.sync_generation_id:
                raise InvalidTransitionError("generation published but cursor does not record it")
            return cursor  # Redelivered publication converges without a second promotion.
        # Fence before anything else: a stale worker may not publish, whatever
        # state it believes the generation is in.
        if current_doc.get("source_fencing_token") != fence.fencing_token:
            raise InvalidTransitionError("stale fencing token cannot publish a generation")
        if current_doc.get("status") != expected_generation_status.value:
            raise InvalidTransitionError(
                f"generation status {current_doc.get('status')} != "
                f"expected {expected_generation_status.value}"
            )
        staged = current_doc.get("staged_manifest", {})
        applied = current_doc.get("applied_manifest", {})
        if (
            staged.get("digest") != expected_staged_manifest.digest
            or int(staged.get("item_count", -1)) != expected_staged_manifest.item_count
            or applied.get("digest") != expected_applied_manifest.digest
            or int(applied.get("item_count", -1)) != expected_applied_manifest.item_count
            or staged.get("digest") != applied.get("digest")
            or int(staged.get("item_count", -1)) != int(applied.get("item_count", -2))
        ):
            raise InvalidTransitionError("staged and applied manifests must match to publish")
        if current_doc.get("outstanding_chunk_id") is not None:
            raise InvalidTransitionError("an apply chunk is still outstanding")
        if (
            generation.mode.value == "full_resync"
            and not bool(current_doc.get("full_sync_tombstones_complete"))
        ):
            raise InvalidTransitionError("full-sync tombstones are incomplete")
        cursor = await self.get(generation.user_id, generation.source)
        if cursor is None:
            raise InvalidTransitionError("cannot publish without a cursor document")
        if cursor.revision != generation.base_published_cursor_revision:
            raise RevisionConflictError(
                "published cursor advanced while this generation was staging"
            )
        if cursor.publish_in_progress_generation_id != generation.sync_generation_id:
            raise InvalidTransitionError("publication barrier is not held by this generation")
        candidate = current_doc.get("candidate_next_cursor")
        promoted = SyncCursor(
            user_id=cursor.user_id,
            source=cursor.source,
            revision=cursor.revision + 1,
            published_cursor=candidate if candidate is not None else cursor.published_cursor,
            published_generation_id=generation.sync_generation_id,
            publish_in_progress_generation_id=None,
            generation_counter=cursor.generation_counter,
            calendar_state_revision=(
                cursor.calendar_state_revision + 1
                if generation.source == SourceType.CALENDAR
                else cursor.calendar_state_revision
            ),
            full_resync_required=False,
            updated_at=published_at,
        )
        await self.save(promoted)
        generation_doc = dict(current_doc)
        generation_doc["status"] = SyncGenerationStatus.PUBLISHED.value
        generation_doc["published_at"] = published_at
        generation_doc["updated_at"] = published_at
        self._context.stage_set(
            SYNC_GENERATIONS, generation.sync_generation_id, generation_doc
        )
        return promoted

    async def mark_full_resync_required(
        self,
        user_id: str,
        source: SourceType,
        expected_cursor_revision: int,
        fence: FencedLease,
        at: datetime,
    ) -> SyncCursor:
        cursor = await self.get(user_id, source)
        if cursor is None:
            raise InvalidTransitionError("cannot mark a missing cursor for full resync")
        if cursor.revision != expected_cursor_revision:
            raise RevisionConflictError(
                f"cursor revision {cursor.revision} != expected {expected_cursor_revision}"
            )
        updated = SyncCursor(
            user_id=cursor.user_id,
            source=cursor.source,
            revision=cursor.revision,
            published_cursor=cursor.published_cursor,
            published_generation_id=cursor.published_generation_id,
            publish_in_progress_generation_id=cursor.publish_in_progress_generation_id,
            generation_counter=cursor.generation_counter,
            calendar_state_revision=cursor.calendar_state_revision,
            full_resync_required=True,
            updated_at=at,
        )
        await self.save(updated)
        return updated

    async def reserve_generation_number(
        self,
        user_id: str,
        source: SourceType,
        now: datetime,
    ) -> tuple[SyncCursor, int]:
        """Increment the monotonic per-cursor generation counter.

        Creates the cursor document at revision 0 when none exists (first
        synchronization for a source). Staged in the same transaction as
        generation creation so two workers cannot reserve the same number.
        """
        cursor = await self.get(user_id, source)
        if cursor is None:
            cursor = SyncCursor(
                user_id=user_id,
                source=source,
                revision=0,
                published_cursor=None,
                published_generation_id=None,
                publish_in_progress_generation_id=None,
                generation_counter=0,
                calendar_state_revision=0,
                full_resync_required=False,
                updated_at=now,
            )
        reserved = cursor.generation_counter + 1
        updated = SyncCursor(
            user_id=cursor.user_id,
            source=cursor.source,
            revision=cursor.revision,
            published_cursor=cursor.published_cursor,
            published_generation_id=cursor.published_generation_id,
            publish_in_progress_generation_id=cursor.publish_in_progress_generation_id,
            generation_counter=reserved,
            calendar_state_revision=cursor.calendar_state_revision,
            full_resync_required=cursor.full_resync_required,
            updated_at=now,
        )
        await self.save(updated)
        return updated, reserved


class FirestoreSyncGenerationRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.sync_generations

    async def get(self, sync_generation_id: str) -> SyncGeneration | None:
        document = await self._context.get(SYNC_GENERATIONS, sync_generation_id)
        if document is None:
            return None
        return self._serializer.from_document(sync_generation_id, document)

    async def get_active(self, user_id: str, source: SourceType) -> SyncGeneration | None:
        rows = await self._context.query(
            SYNC_GENERATIONS,
            [
                ("user_id", "==", user_id),
                ("source", "==", source.value),
                ("status", "in", ACTIVE_GENERATION_STATUSES),
            ],
            limit=2,
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise InvalidTransitionError(
                f"multiple active {source.value} generations for {user_id}"
            )
        document_id, document = rows[0]
        return self._serializer.from_document(document_id, document)

    async def create(
        self,
        generation: SyncGeneration,
        expected_cursor_revision: int,
        fence: FencedLease,
    ) -> bool:
        if generation.base_published_cursor_revision != expected_cursor_revision:
            raise RevisionConflictError("generation base does not match the published cursor")
        if generation.source_fencing_token != fence.fencing_token:
            raise InvalidTransitionError("generation must record the current fencing token")
        current = await self._context.get(SYNC_GENERATIONS, generation.sync_generation_id)
        if current is not None:
            return False
        self._context.stage_create(
            SYNC_GENERATIONS,
            generation.sync_generation_id,
            dict(self._serializer.to_document(generation)),
        )
        return True

    async def adopt_fence(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        """A recovery worker takes over an in-flight generation.

        Only permitted after acquiring the source lease with a newer fencing
        token; the previous worker's token becomes stale for every subsequent
        checkpoint and publication check."""
        document = await self._context.get(SYNC_GENERATIONS, sync_generation_id)
        if document is None:
            raise InvalidTransitionError(f"generation {sync_generation_id} does not exist")
        if document.get("status") not in ACTIVE_GENERATION_STATUSES:
            raise InvalidTransitionError("cannot adopt a terminal generation")
        updated = dict(document)
        updated["source_lease_key"] = fence.lease_key
        updated["source_lease_owner"] = fence.owner
        updated["source_fencing_token"] = fence.fencing_token
        updated["source_lease_expires_at"] = fence.expires_at
        updated["updated_at"] = at
        self._context.stage_set(SYNC_GENERATIONS, sync_generation_id, updated)
        return self._serializer.from_document(sync_generation_id, updated)

    async def _load_checked(
        self,
        sync_generation_id: str,
        fence: FencedLease,
    ) -> tuple[SyncGeneration, dict[str, Any]]:
        document = await self._context.get(SYNC_GENERATIONS, sync_generation_id)
        if document is None:
            raise InvalidTransitionError(f"generation {sync_generation_id} does not exist")
        if document.get("source_fencing_token") != fence.fencing_token:
            raise InvalidTransitionError(
                "stale fencing token cannot checkpoint this generation"
            )
        return self._serializer.from_document(sync_generation_id, document), dict(document)

    async def record_page_checkpoint(
        self,
        checkpoint: SyncPageCheckpoint,
        expected_status: SyncGenerationStatus,
        fence: FencedLease,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(checkpoint.sync_generation_id, fence)
        if generation.current_page_sequence >= checkpoint.page_sequence:
            return generation  # Redelivered page task: checkpoint already committed.
        if generation.status != expected_status:
            raise InvalidTransitionError(
                f"page checkpoint requires {expected_status.value}, "
                f"found {generation.status.value}"
            )
        if checkpoint.page_sequence != generation.current_page_sequence + 1:
            raise InvalidTransitionError(
                f"page {checkpoint.page_sequence} cannot follow "
                f"page {generation.current_page_sequence}"
            )
        document["current_page_sequence"] = checkpoint.page_sequence
        document["page_count"] = generation.page_count + 1
        document["staged_item_count"] = (
            generation.staged_item_count + checkpoint.staged_item_count
        )
        document["staged_manifest"] = {
            "algorithm_version": checkpoint.aggregate_staged_manifest.algorithm_version,
            "item_count": checkpoint.aggregate_staged_manifest.item_count,
            "digest": checkpoint.aggregate_staged_manifest.digest,
        }
        document["next_page_token"] = checkpoint.next_page_token
        if checkpoint.candidate_next_cursor is not None:
            document["candidate_next_cursor"] = checkpoint.candidate_next_cursor
        document["updated_at"] = checkpoint.committed_at
        self._context.stage_set(SYNC_GENERATIONS, checkpoint.sync_generation_id, document)
        return self._serializer.from_document(checkpoint.sync_generation_id, document)

    async def begin_applying(
        self,
        sync_generation_id: str,
        expected_staged_manifest: SyncManifestHash,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(sync_generation_id, fence)
        if generation.status == SyncGenerationStatus.APPLYING:
            return generation  # Resumed apply after worker death or retry.
        if generation.status != SyncGenerationStatus.STAGING:
            raise InvalidTransitionError(
                f"cannot begin applying from {generation.status.value}"
            )
        if generation.next_page_token is not None:
            raise InvalidTransitionError("cannot apply while provider pages remain")
        if (
            generation.staged_manifest.digest != expected_staged_manifest.digest
            or generation.staged_manifest.item_count != expected_staged_manifest.item_count
        ):
            raise InvalidTransitionError("staged manifest mismatch entering apply")
        document["status"] = SyncGenerationStatus.APPLYING.value
        document["updated_at"] = at
        self._context.stage_set(SYNC_GENERATIONS, sync_generation_id, document)
        return self._serializer.from_document(sync_generation_id, document)

    async def record_apply_checkpoint(
        self,
        checkpoint: SyncApplyCheckpoint,
        expected_status: SyncGenerationStatus,
        fence: FencedLease,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(checkpoint.sync_generation_id, fence)
        if generation.status != expected_status:
            raise InvalidTransitionError(
                f"apply checkpoint requires {expected_status.value}, "
                f"found {generation.status.value}"
            )
        document["applied_item_count"] = (
            generation.applied_item_count + checkpoint.applied_item_count
        )
        document["applied_manifest"] = {
            "algorithm_version": checkpoint.aggregate_applied_manifest.algorithm_version,
            "item_count": checkpoint.aggregate_applied_manifest.item_count,
            "digest": checkpoint.aggregate_applied_manifest.digest,
        }
        document["outstanding_chunk_id"] = None
        document["full_sync_tombstones_complete"] = checkpoint.full_sync_tombstones_complete
        document["updated_at"] = checkpoint.committed_at
        self._context.stage_set(SYNC_GENERATIONS, checkpoint.sync_generation_id, document)
        return self._serializer.from_document(checkpoint.sync_generation_id, document)

    async def mark_ready_to_publish(
        self,
        sync_generation_id: str,
        expected_staged_manifest: SyncManifestHash,
        expected_applied_manifest: SyncManifestHash,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(sync_generation_id, fence)
        if generation.status == SyncGenerationStatus.READY_TO_PUBLISH:
            return generation
        if generation.status != SyncGenerationStatus.APPLYING:
            raise InvalidTransitionError(
                f"cannot mark ready to publish from {generation.status.value}"
            )
        if (
            generation.staged_manifest.digest != expected_staged_manifest.digest
            or generation.applied_manifest.digest != expected_applied_manifest.digest
            or generation.staged_manifest.item_count != generation.applied_manifest.item_count
            or generation.staged_manifest.digest != generation.applied_manifest.digest
        ):
            raise InvalidTransitionError("staged and applied manifests must match")
        if (
            generation.mode.value == "full_resync"
            and not generation.full_sync_tombstones_complete
        ):
            raise InvalidTransitionError("full-sync tombstones are incomplete")
        document["status"] = SyncGenerationStatus.READY_TO_PUBLISH.value
        document["updated_at"] = at
        self._context.stage_set(SYNC_GENERATIONS, sync_generation_id, document)
        return self._serializer.from_document(sync_generation_id, document)

    async def mark_full_sync_tombstones_complete(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(sync_generation_id, fence)
        if generation.status != SyncGenerationStatus.APPLYING:
            raise InvalidTransitionError("tombstones complete only while applying")
        document["full_sync_tombstones_complete"] = True
        document["updated_at"] = at
        self._context.stage_set(SYNC_GENERATIONS, sync_generation_id, document)
        return self._serializer.from_document(sync_generation_id, document)

    async def abandon(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        generation, document = await self._load_checked(sync_generation_id, fence)
        if generation.status not in (
            SyncGenerationStatus.STAGING,
            SyncGenerationStatus.APPLYING,
        ):
            raise InvalidTransitionError("only an in-flight generation can be abandoned")
        document["status"] = SyncGenerationStatus.ABANDONED.value
        document["updated_at"] = at
        self._context.stage_set(SYNC_GENERATIONS, sync_generation_id, document)
        return self._serializer.from_document(sync_generation_id, document)


class FirestoreSyncGenerationItemRepository:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializer = serializers.sync_generation_items

    async def _verify_fence(self, sync_generation_id: str, fence: FencedLease) -> None:
        generation = await self._context.get(SYNC_GENERATIONS, sync_generation_id)
        if generation is None:
            raise InvalidTransitionError(f"generation {sync_generation_id} does not exist")
        if generation.get("source_fencing_token") != fence.fencing_token:
            raise InvalidTransitionError("stale fencing token cannot stage or apply items")

    async def stage_chunk(
        self,
        generation: SyncGeneration,
        items: Sequence[SyncGenerationItem],
        chunk_manifest: SyncManifestHash,
        fence: FencedLease,
    ) -> int:
        await self._verify_fence(generation.sync_generation_id, fence)
        staged = 0
        for item in items:
            current = await self._context.get(
                SYNC_GENERATION_ITEMS, item.sync_generation_item_id
            )
            if current is not None:
                # Deterministic IDs make a page retry converge on the same items.
                continue
            self._context.stage_create(
                SYNC_GENERATION_ITEMS,
                item.sync_generation_item_id,
                dict(self._serializer.to_document(item)),
            )
            staged += 1
        return staged

    async def list_staged_for_apply(
        self,
        sync_generation_id: str,
        after_item_id: str | None,
        limit: int,
    ) -> Sequence[SyncGenerationItem]:
        filters: list[tuple[str, str, Any]] = [
            ("sync_generation_id", "==", sync_generation_id),
            ("status", "==", SyncGenerationItemStatus.STAGED.value),
        ]
        if after_item_id is not None:
            filters.append(("sync_generation_item_id", ">", after_item_id))
        rows = await self._context.query(
            SYNC_GENERATION_ITEMS,
            filters,
            order_by=("sync_generation_item_id", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def list_for_generation(
        self,
        sync_generation_id: str,
        after_item_id: str | None,
        limit: int,
    ) -> Sequence[SyncGenerationItem]:
        filters: list[tuple[str, str, Any]] = [
            ("sync_generation_id", "==", sync_generation_id),
        ]
        if after_item_id is not None:
            filters.append(("sync_generation_item_id", ">", after_item_id))
        rows = await self._context.query(
            SYNC_GENERATION_ITEMS,
            filters,
            order_by=("sync_generation_item_id", "ASCENDING"),
            limit=limit,
        )
        return [self._serializer.from_document(doc_id, data) for doc_id, data in rows]

    async def mark_applied(
        self,
        sync_generation_id: str,
        item_ids: Sequence[str],
        checkpoint: SyncApplyCheckpoint,
        fence: FencedLease,
    ) -> None:
        await self._verify_fence(sync_generation_id, fence)
        for item_id in item_ids:
            document = await self._context.get(SYNC_GENERATION_ITEMS, item_id)
            if document is None:
                raise InvalidTransitionError(f"generation item {item_id} does not exist")
            if document.get("sync_generation_id") != sync_generation_id:
                raise InvalidTransitionError(
                    f"generation item {item_id} belongs to another generation"
                )
            updated = dict(document)
            updated["status"] = SyncGenerationItemStatus.APPLIED.value
            updated["applied_at"] = checkpoint.committed_at
            self._context.stage_set(SYNC_GENERATION_ITEMS, item_id, updated)


class FirestoreRepositorySet:
    def __init__(self, context: FirestoreTransactionContext, serializers: SerializerRegistry) -> None:
        self._context = context
        self._serializers = serializers
        self._users = FirestoreUserRepository(context)
        self._commitments = FirestoreCommitmentRepository(context, serializers)
        self._work_blocks = FirestoreWorkBlockRepository(context, serializers)
        self._observations = FirestoreObservationRepository(context, serializers)
        self._evidence = FirestoreEvidenceRepository(context)
        self._source_span_dismissals = FirestoreSourceSpanDismissalRepository(context)
        self._approvals = FirestoreApprovalRepository(context)
        self._outbox = FirestoreActionOutboxRepository(context, serializers)
        self._activity = FirestoreActivityRepository(context, serializers)
        self._sync_requests = FirestoreSyncRequestRepository(context)
        self._calendar_channels = FirestoreCalendarChannelRepository(context)
        self._sync_cursors = FirestoreSyncCursorRepository(context, serializers)
        self._sync_generations = FirestoreSyncGenerationRepository(context, serializers)
        self._sync_generation_items = FirestoreSyncGenerationItemRepository(context, serializers)
        self._processing_leases = FirestoreProcessingLeaseRepository(context)
        self._web_sessions = FirestoreWebSessionRepository(context)
        self._oauth_transactions = FirestoreOAuthTransactionRepository(context, serializers)
        self._planner_runs = FirestorePlannerRunRepository(context, serializers)
        self._reconciliation_runs = FirestoreReconciliationRunRepository(context)
        self._system_controls = FirestoreSystemControlRepository(context, serializers)
        self._calendar_snapshots = FirestoreCalendarSnapshotRepository(context, serializers)

    @property
    def users(self) -> FirestoreUserRepository:
        return self._users

    @property
    def commitments(self) -> FirestoreCommitmentRepository:
        return self._commitments

    @property
    def work_blocks(self) -> FirestoreWorkBlockRepository:
        return self._work_blocks

    @property
    def observations(self) -> FirestoreObservationRepository:
        return self._observations

    @property
    def evidence(self) -> FirestoreEvidenceRepository:
        return self._evidence

    @property
    def source_span_dismissals(self) -> FirestoreSourceSpanDismissalRepository:
        return self._source_span_dismissals

    @property
    def approvals(self) -> FirestoreApprovalRepository:
        return self._approvals

    @property
    def outbox(self) -> FirestoreActionOutboxRepository:
        return self._outbox

    @property
    def activity(self) -> FirestoreActivityRepository:
        return self._activity

    @property
    def sync_requests(self) -> FirestoreSyncRequestRepository:
        return self._sync_requests

    @property
    def calendar_channels(self) -> FirestoreCalendarChannelRepository:
        return self._calendar_channels

    @property
    def sync_cursors(self) -> FirestoreSyncCursorRepository:
        return self._sync_cursors

    @property
    def sync_generations(self) -> FirestoreSyncGenerationRepository:
        return self._sync_generations

    @property
    def sync_generation_items(self) -> FirestoreSyncGenerationItemRepository:
        return self._sync_generation_items

    @property
    def processing_leases(self) -> FirestoreProcessingLeaseRepository:
        return self._processing_leases

    @property
    def web_sessions(self) -> FirestoreWebSessionRepository:
        return self._web_sessions

    @property
    def oauth_transactions(self) -> FirestoreOAuthTransactionRepository:
        return self._oauth_transactions

    @property
    def planner_runs(self) -> FirestorePlannerRunRepository:
        return self._planner_runs

    @property
    def reconciliation_runs(self) -> FirestoreReconciliationRunRepository:
        return self._reconciliation_runs

    @property
    def system_controls(self) -> FirestoreSystemControlRepository:
        return self._system_controls

    @property
    def calendar_snapshots(self) -> FirestoreCalendarSnapshotRepository:
        return self._calendar_snapshots
