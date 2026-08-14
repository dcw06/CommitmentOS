from __future__ import annotations

import base64
import hashlib

from commitmentos.domain.actions.models import CalendarActionType
from commitmentos.domain.shared.types import CanonicalEncoder

EVENT_ID_ALGORITHM = "commitmentos:v1"


class CalendarEventIdFactory:
    def derive(self, calendar_id: str, work_block_id: str) -> str:
        if not calendar_id or not work_block_id:
            raise ValueError("calendar and work-block IDs are required")
        digest = hashlib.sha256(
            CanonicalEncoder.encode([EVENT_ID_ALGORITHM, calendar_id, work_block_id])
        ).digest()
        return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")

    def validate(self, calendar_event_id: str) -> bool:
        return (
            len(calendar_event_id) == 52
            and calendar_event_id.isalnum()
            and calendar_event_id == calendar_event_id.lower()
        )


class ActionIdempotencyKeyFactory:
    def derive(
        self,
        commitment_id: str,
        plan_revision: int,
        action_type: CalendarActionType,
        target_id: str,
    ) -> str:
        if plan_revision <= 0:
            raise ValueError("plan revision must be positive")
        if not commitment_id or not target_id:
            raise ValueError("commitment and target IDs are required")
        return (
            f"action:{commitment_id}:{plan_revision}:"
            f"{action_type.value}:{target_id}"
        )
