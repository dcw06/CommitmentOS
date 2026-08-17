from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from commitmentos.domain.shared.types import CanonicalEncoder


class ObservationType(StrEnum):
    GMAIL_MESSAGE_CHANGED = "gmail_message_changed"
    CALENDAR_EVENT_CHANGED = "calendar_event_changed"
    CALENDAR_ENVIRONMENTAL_DISRUPTION = "calendar_environmental_disruption"
    CALENDAR_USER_MOVE_VALID = "calendar_user_move_valid"
    CALENDAR_USER_MOVE_INVALID = "calendar_user_move_invalid"
    CALENDAR_USER_DELETION = "calendar_user_deletion"
    CALENDAR_APP_ECHO = "calendar_app_echo"
    APPROVAL_RESOLVED = "approval_resolved"
    WORK_CHECK_IN_RECORDED = "work_check_in_recorded"
    PLAN_UNDO_REQUESTED = "plan_undo_requested"
    COMPLETION_CONFIRMED = "completion_confirmed"
    COMMITMENT_REOPENED = "commitment_reopened"
    COMMITMENT_PRIORITY_CHANGED = "commitment_priority_changed"
    COMMITMENT_PAUSED = "commitment_paused"
    COMMITMENT_RESUMED = "commitment_resumed"
    COMMITMENT_DISMISSED = "commitment_dismissed"
    ACTION_RESULT = "action_result"
    SYSTEM_CONTROL_CHANGED = "system_control_changed"
    PERIODIC_SAFETY = "periodic_safety"


class ReconciliationStatus(StrEnum):
    STAGED = "staged"
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    HELD_BY_CONTROL = "held_by_control"
    PROCESSED = "processed"
    IGNORED = "ignored"
    REJECTED = "rejected"
    RETRYABLE_FAILED = "retryable_failed"


TERMINAL_RECONCILIATION_STATUSES = frozenset(
    {
        ReconciliationStatus.PROCESSED,
        ReconciliationStatus.IGNORED,
        ReconciliationStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class ObservationV1:
    observation_id: str
    observation_type: ObservationType
    user_id: str
    producer_id: str
    producer_version: str
    source: str
    external_id: str
    external_version: str
    observed_at: datetime
    payload_hash: str
    source_reference: Mapping[str, str]
    safe_metadata: Mapping[str, Any]
    reconciliation_status: ReconciliationStatus
    dispatch_generation: int
    held_control_epoch: int | None
    claimed_control_epoch: int | None
    processing_lease_key: str | None
    processing_lease_owner: str | None
    processing_fencing_token: str | None
    processing_lease_expires_at: datetime | None
    processing_attempt: int
    trace_id: str


class ObservationIdFactory:
    def create(
        self,
        observation_type: ObservationType,
        producer_id: str,
        producer_version: str,
    ) -> str:
        return CanonicalEncoder.hash(
            ["observation:v1", observation_type.value, producer_id, producer_version]
        )


class ObservationFactory:
    def __init__(self, id_factory: ObservationIdFactory | None = None) -> None:
        self._id_factory = id_factory if id_factory is not None else ObservationIdFactory()

    def source_change(
        self,
        observation_type: ObservationType,
        user_id: str,
        producer_id: str,
        producer_version: str,
        source: str,
        external_id: str,
        external_version: str,
        payload_hash: str,
        source_reference: Mapping[str, str],
        safe_metadata: Mapping[str, Any],
        observed_at: datetime,
        trace_id: str,
        *,
        reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING,
    ) -> ObservationV1:
        return ObservationV1(
            observation_id=self._id_factory.create(observation_type, producer_id, producer_version),
            observation_type=observation_type,
            user_id=user_id,
            producer_id=producer_id,
            producer_version=producer_version,
            source=source,
            external_id=external_id,
            external_version=external_version,
            observed_at=observed_at,
            payload_hash=payload_hash,
            source_reference=dict(source_reference),
            safe_metadata=dict(safe_metadata),
            reconciliation_status=reconciliation_status,
            dispatch_generation=0,
            held_control_epoch=None,
            claimed_control_epoch=None,
            processing_lease_key=None,
            processing_lease_owner=None,
            processing_fencing_token=None,
            processing_lease_expires_at=None,
            processing_attempt=0,
            trace_id=trace_id,
        )

    def continuation(
        self,
        observation_type: ObservationType,
        user_id: str,
        producer_id: str,
        producer_version: str,
        safe_metadata: Mapping[str, Any],
        observed_at: datetime,
        trace_id: str,
    ) -> ObservationV1:
        return self.source_change(
            observation_type=observation_type,
            user_id=user_id,
            producer_id=producer_id,
            producer_version=producer_version,
            source="internal",
            external_id=producer_id,
            external_version=producer_version,
            payload_hash="",
            source_reference={},
            safe_metadata=safe_metadata,
            observed_at=observed_at,
            trace_id=trace_id,
        )
