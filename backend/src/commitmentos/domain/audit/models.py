from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from commitmentos.domain.shared.types import CanonicalEncoder


class ActivityEventType(StrEnum):
    OBSERVATION_RECEIVED = "observation_received"
    INTERPRETATION_CREATED = "interpretation_created"
    INTERPRETATION_REJECTED = "interpretation_rejected"
    CONFIRMATION_RECORDED = "confirmation_recorded"
    RISK_CHANGED = "risk_changed"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_REPAIRED = "plan_repaired"
    POLICY_DECIDED = "policy_decided"
    OUTBOX_WRITTEN = "outbox_written"
    CALENDAR_ACTION_RESULT = "calendar_action_result"
    USER_MOVE_ADOPTED = "user_move_adopted"
    USER_MOVE_REQUIRES_DECISION = "user_move_requires_decision"
    USER_DELETION_REQUIRES_DECISION = "user_deletion_requires_decision"
    CONTROL_CHANGED = "control_changed"
    WORK_CHECK_IN_RECORDED = "work_check_in_recorded"
    WORK_BLOCK_ACTIVATED = "work_block_activated"
    WORK_CHECK_IN_REQUIRED = "work_check_in_required"
    PLAN_UNDO_REQUESTED = "plan_undo_requested"
    COMPLETION_RECORDED = "completion_recorded"
    FAILURE_RECORDED = "failure_recorded"


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    activity_event_id: str
    user_id: str
    event_type: ActivityEventType
    trace_id: str
    actor: str
    summary: str
    payload: Mapping[str, Any]
    created_at: datetime


class ActivityEventFactory:
    def create(
        self,
        user_id: str,
        event_type: ActivityEventType,
        trace_id: str,
        actor: str,
        summary: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> ActivityEvent:
        # Deterministic identity: replaying the transaction that records this
        # event converges on the same audit document instead of duplicating it.
        payload_hash = CanonicalEncoder.hash(
            ["activity-payload:v1", json.dumps(payload, sort_keys=True, default=str)]
        )
        event_id = CanonicalEncoder.hash(
            [
                "activity:v1",
                user_id,
                event_type.value,
                trace_id,
                actor,
                summary,
                payload_hash,
                created_at,
            ]
        )
        return ActivityEvent(
            activity_event_id=event_id,
            user_id=user_id,
            event_type=event_type,
            trace_id=trace_id,
            actor=actor,
            summary=summary,
            payload=dict(payload),
            created_at=created_at,
        )
