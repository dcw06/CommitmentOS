from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from commitmentos.application.dto import FencedLease
from commitmentos.contracts.auth import OAuthTransaction
from commitmentos.contracts.observations import ObservationV1, ReconciliationStatus
from commitmentos.contracts.synchronization import (
    SyncApplyCheckpoint,
    SyncCursor,
    SyncGeneration,
    SyncGenerationItem,
    SyncGenerationStatus,
    SyncManifestHash,
    SyncPageCheckpoint,
    SyncPublicationBarrier,
)
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.actions.models import ActionOutbox
from commitmentos.domain.audit.models import ActivityEvent
from commitmentos.domain.commitments.models import Commitment, LifecycleStatus
from commitmentos.domain.controls.models import SystemControls
from commitmentos.domain.planning.calendar_state import CalendarEventSnapshot, CalendarStateSnapshot
from commitmentos.domain.planning.models import PortfolioPlan
from commitmentos.domain.progress.models import WorkBlock


class UserRepository(Protocol):
    async def get(self, user_id: str) -> Mapping[str, Any] | None:
        ...

    async def save(self, user_id: str, user: Mapping[str, Any]) -> None:
        ...


class CommitmentRepository(Protocol):
    async def get(self, commitment_id: str) -> Commitment | None:
        ...

    async def list_active(self, user_id: str) -> Sequence[Commitment]:
        ...

    async def list_for_thread(self, user_id: str, thread_id: str) -> Sequence[Commitment]:
        ...

    async def list_for_user(
        self,
        user_id: str,
        lifecycle_status: LifecycleStatus | None,
        before: datetime | None,
        limit: int,
    ) -> Sequence[Commitment]:
        ...

    async def save(self, commitment: Commitment, expected_revision: int | None) -> None:
        ...


class WorkBlockRepository(Protocol):
    async def get(self, work_block_id: str) -> WorkBlock | None:
        ...

    async def list_for_commitment(self, commitment_id: str) -> Sequence[WorkBlock]:
        ...

    async def list_for_user_horizon(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[WorkBlock]:
        ...

    async def save(self, work_block: WorkBlock, expected_revision: int | None) -> None:
        ...


class ObservationRepository(Protocol):
    async def get(self, observation_id: str) -> ObservationV1 | None:
        ...

    async def create(self, observation: ObservationV1) -> bool:
        ...

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
        ...

    async def list_pending(self, user_id: str, limit: int) -> Sequence[ObservationV1]:
        ...

    async def list_held(self, user_id: str, limit: int) -> Sequence[ObservationV1]:
        ...

    async def list_for_statuses(
        self,
        user_id: str,
        reconciliation_statuses: Sequence[str],
        limit: int,
    ) -> Sequence[ObservationV1]:
        ...

    async def list_staged_for_release(
        self,
        user_id: str,
        limit: int,
    ) -> Sequence[ObservationV1]:
        ...


class EvidenceRepository(Protocol):
    async def get(self, evidence_id: str) -> Mapping[str, Any] | None:
        ...

    async def create(self, evidence: Mapping[str, Any]) -> bool:
        ...

    async def list_for_commitment(self, commitment_id: str) -> Sequence[Mapping[str, Any]]:
        ...


class SourceSpanDismissalRepository(Protocol):
    async def get(self, dismissal_id: str) -> Mapping[str, Any] | None:
        ...

    async def create(self, dismissal: Mapping[str, Any]) -> bool:
        ...

    async def list_for_thread(
        self,
        user_id: str,
        thread_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        ...


class ApprovalRepository(Protocol):
    async def get(self, approval_id: str) -> Mapping[str, Any] | None:
        ...

    async def create(self, approval: Mapping[str, Any]) -> bool:
        ...

    async def resolve(
        self,
        approval_id: str,
        expected_revision: int,
        decision: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    async def list_pending(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        ...


class ActionOutboxRepository(Protocol):
    async def get(self, outbox_id: str) -> ActionOutbox | None:
        ...

    async def create(self, action: ActionOutbox) -> bool:
        ...

    async def save(self, action: ActionOutbox, expected_updated_at: datetime) -> None:
        ...

    async def list_pending_dispatch(self, user_id: str, limit: int) -> Sequence[ActionOutbox]:
        ...

    async def list_held(self, user_id: str, limit: int) -> Sequence[ActionOutbox]:
        ...

    async def list_for_work_block(
        self,
        user_id: str,
        work_block_id: str,
        limit: int,
    ) -> Sequence[ActionOutbox]:
        ...

    async def list_for_user_statuses(
        self,
        user_id: str,
        execution_statuses: Sequence[str],
        limit: int,
    ) -> Sequence[ActionOutbox]:
        ...


class ActivityRepository(Protocol):
    async def append(self, event: ActivityEvent) -> None:
        ...

    async def list_for_user(
        self,
        user_id: str,
        before: datetime | None,
        limit: int,
    ) -> Sequence[ActivityEvent]:
        ...


class SyncRequestRepository(Protocol):
    async def get(self, sync_request_id: str) -> Mapping[str, Any] | None:
        ...

    async def upsert(self, sync_request_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    async def list_pending(self, limit: int) -> Sequence[Mapping[str, Any]]:
        ...

    async def list_for_user(
        self, user_id: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        ...


class CalendarChannelRepository(Protocol):
    async def get(self, user_id: str) -> Mapping[str, Any] | None:
        ...

    async def get_by_channel_id(self, channel_id: str) -> Mapping[str, Any] | None:
        ...

    async def save(self, user_id: str, channel: Mapping[str, Any]) -> None:
        ...

    async def consume_rate_limit(
        self,
        channel_id: str,
        now: datetime,
        window_seconds: int,
        maximum_signals: int,
    ) -> bool:
        ...


class SyncCursorRepository(Protocol):
    async def get(self, user_id: str, source: SourceType) -> SyncCursor | None:
        ...

    async def save(self, cursor: SyncCursor) -> None:
        ...

    async def reserve_generation_number(
        self,
        user_id: str,
        source: SourceType,
        now: datetime,
    ) -> tuple[SyncCursor, int]:
        ...

    async def activate_publication_barrier(
        self,
        barrier: SyncPublicationBarrier,
        expected_cursor_revision: int,
        fence: FencedLease,
    ) -> SyncCursor:
        ...

    async def publish_generation(
        self,
        generation: SyncGeneration,
        expected_generation_status: SyncGenerationStatus,
        expected_staged_manifest: SyncManifestHash,
        expected_applied_manifest: SyncManifestHash,
        fence: FencedLease,
        published_at: datetime,
    ) -> SyncCursor:
        ...

    async def mark_full_resync_required(
        self,
        user_id: str,
        source: SourceType,
        expected_cursor_revision: int,
        fence: FencedLease,
        at: datetime,
    ) -> SyncCursor:
        ...


class SyncGenerationRepository(Protocol):
    async def get(self, sync_generation_id: str) -> SyncGeneration | None:
        ...

    async def get_active(self, user_id: str, source: SourceType) -> SyncGeneration | None:
        ...

    async def adopt_fence(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        ...

    async def create(
        self,
        generation: SyncGeneration,
        expected_cursor_revision: int,
        fence: FencedLease,
    ) -> bool:
        ...

    async def record_page_checkpoint(
        self,
        checkpoint: SyncPageCheckpoint,
        expected_status: SyncGenerationStatus,
        fence: FencedLease,
    ) -> SyncGeneration:
        ...

    async def begin_applying(
        self,
        sync_generation_id: str,
        expected_staged_manifest: SyncManifestHash,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        ...

    async def record_apply_checkpoint(
        self,
        checkpoint: SyncApplyCheckpoint,
        expected_status: SyncGenerationStatus,
        fence: FencedLease,
    ) -> SyncGeneration:
        ...

    async def mark_ready_to_publish(
        self,
        sync_generation_id: str,
        expected_staged_manifest: SyncManifestHash,
        expected_applied_manifest: SyncManifestHash,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        ...

    async def mark_full_sync_tombstones_complete(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        ...

    async def abandon(
        self,
        sync_generation_id: str,
        fence: FencedLease,
        at: datetime,
    ) -> SyncGeneration:
        ...


class SyncGenerationItemRepository(Protocol):
    async def stage_chunk(
        self,
        generation: SyncGeneration,
        items: Sequence[SyncGenerationItem],
        chunk_manifest: SyncManifestHash,
        fence: FencedLease,
    ) -> int:
        ...

    async def list_staged_for_apply(
        self,
        sync_generation_id: str,
        after_item_id: str | None,
        limit: int,
    ) -> Sequence[SyncGenerationItem]:
        ...

    async def list_for_generation(
        self,
        sync_generation_id: str,
        after_item_id: str | None,
        limit: int,
    ) -> Sequence[SyncGenerationItem]:
        ...

    async def mark_applied(
        self,
        sync_generation_id: str,
        item_ids: Sequence[str],
        checkpoint: SyncApplyCheckpoint,
        fence: FencedLease,
    ) -> None:
        ...


class ProcessingLeaseRepository(Protocol):
    async def acquire(
        self,
        lease_key: str,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> FencedLease | None:
        ...

    async def release(self, lease_key: str, owner: str, fencing_token: str) -> None:
        ...

    async def verify(self, fence: FencedLease, at: datetime) -> None:
        ...


class WebSessionRepository(Protocol):
    async def get_by_hash(self, session_id_hash: str) -> Mapping[str, Any] | None:
        ...

    async def create(self, session: Mapping[str, Any]) -> None:
        ...

    async def revoke(self, session_id_hash: str, revoked_at: datetime) -> None:
        ...


class OAuthTransactionRepository(Protocol):
    async def create(self, transaction: OAuthTransaction) -> bool:
        ...

    async def consume_pending(
        self,
        state_hash: str,
        consumed_at: datetime,
    ) -> OAuthTransaction:
        ...

    async def delete_expired(self, expired_before: datetime, limit: int) -> int:
        ...


class PlannerRunRepository(Protocol):
    async def get(self, planner_run_id: str) -> PortfolioPlan | None:
        ...

    async def create(self, plan: PortfolioPlan) -> bool:
        ...

    async def save(self, plan: PortfolioPlan, expected_status: str) -> None:
        ...

    async def list_for_user(
        self,
        user_id: str,
        status: str | None,
        limit: int,
    ) -> Sequence[PortfolioPlan]:
        ...


class ReconciliationRunRepository(Protocol):
    async def get(self, run_id: str) -> Mapping[str, Any] | None:
        ...

    async def save(
        self,
        run_id: str,
        run: Mapping[str, Any],
        expected_processing_fencing_token: str,
    ) -> None:
        ...


class SystemControlRepository(Protocol):
    async def get(self, user_id: str) -> SystemControls:
        ...

    async def save(self, controls: SystemControls, expected_epoch: int) -> None:
        ...


class CalendarSnapshotRepository(Protocol):
    async def get(self, calendar_id: str, calendar_event_id: str) -> CalendarEventSnapshot | None:
        ...

    async def save(self, snapshot: CalendarEventSnapshot) -> None:
        ...

    async def list_for_calendar(
        self,
        user_id: str,
        calendar_id: str,
        after_snapshot_id: str | None,
        limit: int,
    ) -> Sequence[CalendarEventSnapshot]:
        ...

    async def load_consistent_state(
        self,
        user_id: str,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> CalendarStateSnapshot:
        ...
