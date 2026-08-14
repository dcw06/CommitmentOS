from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from commitmentos.application.dto import FencedLease
from commitmentos.application.ports.repositories import (
    ActionOutboxRepository,
    ActivityRepository,
    ApprovalRepository,
    CalendarChannelRepository,
    CalendarSnapshotRepository,
    CommitmentRepository,
    EvidenceRepository,
    OAuthTransactionRepository,
    ObservationRepository,
    PlannerRunRepository,
    ProcessingLeaseRepository,
    ReconciliationRunRepository,
    SourceSpanDismissalRepository,
    SyncCursorRepository,
    SyncGenerationItemRepository,
    SyncGenerationRepository,
    SyncRequestRepository,
    SystemControlRepository,
    UserRepository,
    WebSessionRepository,
    WorkBlockRepository,
)

T = TypeVar("T")


class RepositorySet(Protocol):
    @property
    def users(self) -> UserRepository:
        ...

    @property
    def commitments(self) -> CommitmentRepository:
        ...

    @property
    def work_blocks(self) -> WorkBlockRepository:
        ...

    @property
    def observations(self) -> ObservationRepository:
        ...

    @property
    def evidence(self) -> EvidenceRepository:
        ...

    @property
    def source_span_dismissals(self) -> SourceSpanDismissalRepository:
        ...

    @property
    def approvals(self) -> ApprovalRepository:
        ...

    @property
    def outbox(self) -> ActionOutboxRepository:
        ...

    @property
    def activity(self) -> ActivityRepository:
        ...

    @property
    def sync_requests(self) -> SyncRequestRepository:
        ...

    @property
    def calendar_channels(self) -> CalendarChannelRepository:
        ...

    @property
    def sync_cursors(self) -> SyncCursorRepository:
        ...

    @property
    def sync_generations(self) -> SyncGenerationRepository:
        ...

    @property
    def sync_generation_items(self) -> SyncGenerationItemRepository:
        ...

    @property
    def processing_leases(self) -> ProcessingLeaseRepository:
        ...

    @property
    def web_sessions(self) -> WebSessionRepository:
        ...

    @property
    def oauth_transactions(self) -> OAuthTransactionRepository:
        ...

    @property
    def planner_runs(self) -> PlannerRunRepository:
        ...

    @property
    def reconciliation_runs(self) -> ReconciliationRunRepository:
        ...

    @property
    def system_controls(self) -> SystemControlRepository:
        ...

    @property
    def calendar_snapshots(self) -> CalendarSnapshotRepository:
        ...


class UnitOfWork(Protocol):
    async def run(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        ...

    async def read(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        ...

    async def run_fenced(
        self,
        fence: FencedLease,
        operation: Callable[[RepositorySet], Awaitable[T]],
    ) -> T:
        ...
