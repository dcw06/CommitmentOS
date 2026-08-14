from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from commitmentos.application.ports.calendar_writer import (
    CalendarMutationOutcomeType,
    CalendarWriter,
)
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.domain.actions.models import CalendarActionType, CalendarMutation
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType

# Domain state the between-runs reset owns. Deliberately excluded: activity
# and reconciliation-run audit history (retained per plan §13.3), sync
# cursors/generations and calendar channels (source truth machinery), web
# sessions and system controls (login and execution-control state), and
# calendar snapshots (observed provider truth — the sync loop tombstones
# canceled events itself rather than the cleanup pretending to know).
PURGED_USER_COLLECTIONS = (
    "commitments",
    "evidence",
    "approvals",
    "source_observations",
    "action_outbox",
    "planner_runs",
    "source_span_dismissals",
)
PURGE_BATCH_LIMIT = 200


class CleanupDocumentStore(Protocol):
    """Bounded document enumeration and deletion for the cleanup command.

    Backed by the raw Firestore client in the script entry and by the
    in-memory store in tests; the command itself never issues an unbounded
    operation through it.
    """

    async def list_ids_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
        limit: int,
    ) -> Sequence[str]:
        ...

    async def count_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
    ) -> int:
        ...

    async def purge_where(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
        limit: int,
    ) -> int:
        ...


@dataclass(frozen=True, slots=True)
class OwnedEventTarget:
    calendar_id: str
    calendar_event_id: str
    work_block_id: str
    commitment_id: str
    expected_observed_event_etag: str | None


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    user_id: str
    document_counts: Mapping[str, int]
    owned_event_targets: tuple[OwnedEventTarget, ...]

    def total_documents(self) -> int:
        return sum(self.document_counts.values())


@dataclass(frozen=True, slots=True)
class CleanupResult:
    executed: bool
    abort_reason: str | None
    events_canceled: int
    events_already_absent: int
    events_skipped_stale_or_unsynchronized: int
    documents_purged: Mapping[str, int]


class CleanupControlledAccount:
    """Audited developer cleanup for the controlled account (D4, plan §13.3).

    Preview-then-execute with a typed confirmation phrase: execution aborts
    if durable state drifted since the preview. Calendar deletion targets
    only recorded app-owned work-block events, sends the snapshot etag as
    `If-Match`, and relies on the writer's independent §9.3 ownership guard;
    unrelated events are structurally unreachable. The cleanup itself is
    recorded in the retained audit timeline.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        calendar_writer: CalendarWriter,
        document_store: CleanupDocumentStore,
        clock: Clock,
        activity_factory: ActivityEventFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._calendar_writer = calendar_writer
        self._document_store = document_store
        self._clock = clock
        self._activity_factory = activity_factory or ActivityEventFactory()

    async def preview(self, user_id: str) -> CleanupPreview:
        user_filter = (("user_id", "==", user_id),)
        counts: dict[str, int] = {}
        for collection in PURGED_USER_COLLECTIONS:
            if collection == "evidence":
                continue
            counts[collection] = await self._document_store.count_where(
                collection, user_filter
            )
        commitment_ids = tuple(
            await self._document_store.list_ids_where(
                "commitments", user_filter, PURGE_BATCH_LIMIT
            )
        )
        # Evidence may be linked by user or by commitment only (the seeded
        # path writes commitment-linked evidence); count the union.
        evidence_ids = set(
            await self._document_store.list_ids_where(
                "evidence", user_filter, PURGE_BATCH_LIMIT
            )
        )
        for commitment_id in commitment_ids:
            evidence_ids.update(
                await self._document_store.list_ids_where(
                    "evidence",
                    (("commitment_id", "==", commitment_id),),
                    PURGE_BATCH_LIMIT,
                )
            )
        counts["evidence"] = len(evidence_ids)
        targets: list[OwnedEventTarget] = []
        work_block_count = 0
        for commitment_id in commitment_ids:

            async def _blocks_with_snapshots(
                repositories: RepositorySet,
                commitment_id: str = commitment_id,
            ) -> list[OwnedEventTarget]:
                found: list[OwnedEventTarget] = []
                for block in await repositories.work_blocks.list_for_commitment(
                    commitment_id
                ):
                    snapshot = await repositories.calendar_snapshots.get(
                        block.calendar_id, block.calendar_event_id
                    )
                    if snapshot is not None and snapshot.is_tombstone:
                        continue
                    found.append(
                        OwnedEventTarget(
                            calendar_id=block.calendar_id,
                            calendar_event_id=block.calendar_event_id,
                            work_block_id=block.work_block_id,
                            commitment_id=commitment_id,
                            expected_observed_event_etag=(
                                snapshot.observed_event_etag
                                if snapshot is not None
                                else None
                            ),
                        )
                    )
                return found

            block_targets = await self._unit_of_work.read(_blocks_with_snapshots)
            work_block_count += await self._document_store.count_where(
                "work_blocks", (("commitment_id", "==", commitment_id),)
            )
            targets.extend(block_targets)
        counts["work_blocks"] = work_block_count
        return CleanupPreview(
            user_id=user_id,
            document_counts=counts,
            owned_event_targets=tuple(
                sorted(targets, key=lambda item: item.calendar_event_id)
            ),
        )

    def confirmation_phrase(self, preview: CleanupPreview) -> str:
        return (
            f"cleanup {preview.user_id} "
            f"events={len(preview.owned_event_targets)} "
            f"documents={preview.total_documents()}"
        )

    async def execute(
        self,
        user_id: str,
        expected_preview: CleanupPreview,
        confirmation: str,
        trace_id: str,
    ) -> CleanupResult:
        if confirmation != self.confirmation_phrase(expected_preview):
            return self._aborted("confirmation_mismatch")
        current = await self.preview(user_id)
        if current != expected_preview:
            # Durable state moved between preview and execute; a stale
            # preview must never authorize deletion of newer state.
            return self._aborted("state_changed_since_preview")

        canceled = 0
        already_absent = 0
        skipped = 0
        for target in current.owned_event_targets:
            if target.expected_observed_event_etag is None:
                # No synchronized snapshot: never delete without `If-Match`
                # truth. The sync loop keeps provider state honest instead.
                skipped += 1
                continue
            outcome = await self._calendar_writer.cancel_owned(
                CalendarMutation(
                    action_type=CalendarActionType.CANCEL,
                    calendar_id=target.calendar_id,
                    calendar_event_id=target.calendar_event_id,
                    work_block_id=target.work_block_id,
                    desired_start=None,
                    desired_end=None,
                    expected_observed_event_etag=target.expected_observed_event_etag,
                    private_properties={
                        "managed_by": "commitmentos",
                        "commitment_id": target.commitment_id,
                        "work_block_id": target.work_block_id,
                    },
                )
            )
            if outcome.outcome_type in (
                CalendarMutationOutcomeType.APPLIED,
                CalendarMutationOutcomeType.ALREADY_APPLIED,
            ):
                canceled += 1
            elif outcome.outcome_type == CalendarMutationOutcomeType.TERMINAL_FAILURE:
                already_absent += 1
            else:
                # Precondition or transient failure: skip without a blind
                # retry; the next synchronization resolves actual truth.
                skipped += 1

        purged: dict[str, int] = {}
        commitment_filter_ids = tuple(
            await self._document_store.list_ids_where(
                "commitments", (("user_id", "==", user_id),), PURGE_BATCH_LIMIT
            )
        )
        work_blocks_purged = 0
        evidence_purged = 0
        for commitment_id in commitment_filter_ids:
            commitment_filter = (("commitment_id", "==", commitment_id),)
            work_blocks_purged += await self._purge_all("work_blocks", commitment_filter)
            evidence_purged += await self._purge_all("evidence", commitment_filter)
        purged["work_blocks"] = work_blocks_purged
        for collection in PURGED_USER_COLLECTIONS:
            purged[collection] = await self._purge_all(
                collection, (("user_id", "==", user_id),)
            )
        purged["evidence"] += evidence_purged

        now = self._clock.now()
        result = CleanupResult(
            executed=True,
            abort_reason=None,
            events_canceled=canceled,
            events_already_absent=already_absent,
            events_skipped_stale_or_unsynchronized=skipped,
            documents_purged=purged,
        )

        async def _record(repositories: RepositorySet) -> None:
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=user_id,
                    event_type=ActivityEventType.CONTROLLED_CLEANUP_COMPLETED,
                    trace_id=trace_id,
                    actor="developer_cleanup",
                    summary=(
                        "Controlled-account cleanup: "
                        f"{canceled} owned event(s) canceled, "
                        f"{sum(purged.values())} document(s) purged"
                    ),
                    payload={
                        "events_canceled": canceled,
                        "events_already_absent": already_absent,
                        "events_skipped_stale_or_unsynchronized": skipped,
                        "documents_purged": purged,
                        "targeted_event_ids": [
                            target.calendar_event_id
                            for target in current.owned_event_targets
                        ],
                    },
                    created_at=now,
                )
            )

        await self._unit_of_work.run(_record)
        return result

    async def _purge_all(
        self,
        collection: str,
        filters: Sequence[tuple[str, str, Any]],
    ) -> int:
        total = 0
        while True:
            removed = await self._document_store.purge_where(
                collection, filters, PURGE_BATCH_LIMIT
            )
            total += removed
            if removed < PURGE_BATCH_LIMIT:
                return total

    @staticmethod
    def _aborted(reason: str) -> CleanupResult:
        return CleanupResult(
            executed=False,
            abort_reason=reason,
            events_canceled=0,
            events_already_absent=0,
            events_skipped_stale_or_unsynchronized=0,
            documents_purged={},
        )
