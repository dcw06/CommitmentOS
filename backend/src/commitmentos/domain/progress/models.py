from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from commitmentos.domain.shared.errors import InvalidTransitionError


class WorkBlockExecutionState(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    AWAITING_CHECK_IN = "awaiting_check_in"
    COMPLETED = "completed"
    MISSED = "missed"
    CANCELED = "canceled"


class UserEditState(StrEnum):
    NONE = "none"
    ADOPTED = "adopted"
    INVALID_MOVE = "invalid_move"
    USER_DELETED = "user_deleted"


@dataclass(frozen=True, slots=True)
class WorkBlock:
    work_block_id: str
    commitment_id: str
    revision: int
    calendar_id: str
    calendar_event_id: str
    calendar_snapshot_id: str | None
    duration_minutes: int
    execution_state: WorkBlockExecutionState
    scheduled_start: datetime
    scheduled_end: datetime
    verified_minutes: int
    completion_evidence_id: str | None
    user_edit_state: UserEditState
    plan_revision: int

    def _require_state(self, *allowed: WorkBlockExecutionState) -> None:
        if self.execution_state not in allowed:
            raise InvalidTransitionError(
                f"work block {self.work_block_id} is {self.execution_state}; "
                f"expected one of {[state.value for state in allowed]}"
            )

    def activate(self, at: datetime) -> WorkBlock:
        self._require_state(WorkBlockExecutionState.PLANNED)
        return replace(
            self,
            execution_state=WorkBlockExecutionState.ACTIVE,
            revision=self.revision + 1,
        )

    def request_check_in(self, at: datetime) -> WorkBlock:
        # An elapsed block never silently reduces remaining effort; it asks for
        # an explicit check-in instead (plan §4.4).
        self._require_state(WorkBlockExecutionState.PLANNED, WorkBlockExecutionState.ACTIVE)
        return replace(
            self,
            execution_state=WorkBlockExecutionState.AWAITING_CHECK_IN,
            revision=self.revision + 1,
        )

    def check_in(self, verified_minutes: int, evidence_id: str, at: datetime) -> WorkBlock:
        return self.record_check_in(
            completed=True,
            verified_minutes=verified_minutes,
            evidence_id=evidence_id,
            at=at,
        )

    def record_check_in(
        self,
        completed: bool,
        verified_minutes: int,
        evidence_id: str,
        at: datetime,
    ) -> WorkBlock:
        self._require_state(
            WorkBlockExecutionState.ACTIVE,
            WorkBlockExecutionState.AWAITING_CHECK_IN,
        )
        if verified_minutes < 0 or verified_minutes > self.duration_minutes:
            raise InvalidTransitionError(
                "verified minutes must be between zero and the block duration"
            )
        if not evidence_id:
            raise InvalidTransitionError("a check-in requires an evidence reference")
        return replace(
            self,
            execution_state=(
                WorkBlockExecutionState.COMPLETED
                if completed
                else WorkBlockExecutionState.MISSED
            ),
            verified_minutes=verified_minutes,
            completion_evidence_id=evidence_id,
            revision=self.revision + 1,
        )

    def mark_missed(self, at: datetime) -> WorkBlock:
        self._require_state(WorkBlockExecutionState.AWAITING_CHECK_IN)
        return replace(
            self,
            execution_state=WorkBlockExecutionState.MISSED,
            revision=self.revision + 1,
        )

    def cancel(self, at: datetime) -> WorkBlock:
        self._require_state(WorkBlockExecutionState.PLANNED)
        return replace(
            self,
            execution_state=WorkBlockExecutionState.CANCELED,
            revision=self.revision + 1,
        )

    def adopt_manual_move(self, start: datetime, end: datetime, at: datetime) -> WorkBlock:
        self._require_state(WorkBlockExecutionState.PLANNED)
        if end <= start:
            raise InvalidTransitionError("a work block must end after it starts")
        return replace(
            self,
            scheduled_start=start,
            scheduled_end=end,
            user_edit_state=UserEditState.ADOPTED,
            revision=self.revision + 1,
        )

    def mark_invalid_move(self, at: datetime) -> WorkBlock:
        """Record that Calendar truth cannot be silently adopted.

        The approved interval remains untouched until the user chooses how to
        resolve the hard-constraint breach.
        """
        self._require_state(WorkBlockExecutionState.PLANNED)
        return replace(
            self,
            user_edit_state=UserEditState.INVALID_MOVE,
            revision=self.revision + 1,
        )

    def mark_user_deleted(self, at: datetime) -> WorkBlock:
        """Record an external deletion without recreating or cancelling it."""
        self._require_state(WorkBlockExecutionState.PLANNED)
        return replace(
            self,
            user_edit_state=UserEditState.USER_DELETED,
            revision=self.revision + 1,
        )
