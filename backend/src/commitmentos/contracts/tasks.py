from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from commitmentos.domain.shared.types import CanonicalEncoder


class SourceType(StrEnum):
    GMAIL = "gmail"
    CALENDAR = "calendar"


@dataclass(frozen=True, slots=True)
class SourceSyncTaskV1:
    schema_version: str
    sync_request_id: str
    sync_generation_id: str
    page_sequence: int
    source: SourceType
    user_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class ReconcileObservationTaskV1:
    schema_version: str
    observation_id: str
    workflow_version: str
    dispatch_generation: int
    trace_id: str


@dataclass(frozen=True, slots=True)
class ExecuteCalendarActionTaskV1:
    schema_version: str
    outbox_id: str
    action_idempotency_key: str
    trace_id: str


class TaskNameFactory:
    """Deterministic Cloud Tasks names.

    Task names are a transport deduplication aid only; Firestore IDs and
    revision checks remain the product-level idempotency boundary. Names must
    be stable across retries of the same logical work and differ across
    dispatch generations because Cloud Tasks may retain a completed name for
    up to 24 hours.
    """

    def source_sync(self, task: SourceSyncTaskV1) -> str:
        digest = CanonicalEncoder.hash(
            [
                "task:source-sync:v1",
                task.source.value,
                task.user_id,
                task.sync_generation_id,
                task.page_sequence,
            ]
        )
        return f"srcsync-{digest[:40]}"

    def reconciliation(self, task: ReconcileObservationTaskV1) -> str:
        digest = CanonicalEncoder.hash(
            [
                "task:reconciliation:v1",
                task.observation_id,
                task.workflow_version,
                task.dispatch_generation,
            ]
        )
        return f"reconcile-{digest[:40]}"

    def calendar_action(self, task: ExecuteCalendarActionTaskV1) -> str:
        digest = CanonicalEncoder.hash(
            [
                "task:calendar-action:v1",
                task.outbox_id,
                task.action_idempotency_key,
            ]
        )
        return f"calaction-{digest[:40]}"
