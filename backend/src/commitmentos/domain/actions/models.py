from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from commitmentos.domain.shared.errors import InvalidTransitionError


class CalendarActionType(StrEnum):
    INSERT = "insert"
    PATCH = "patch"
    ADOPT = "adopt"
    CANCEL = "cancel"


class DispatchStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    DELIVERED = "delivered"
    HELD_BY_CONTROL = "held_by_control"
    SUPERSEDED = "superseded"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACTION_IN_FLIGHT = "action_in_flight"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    HELD_BY_CONTROL = "held_by_control"
    STALE = "stale"
    STALE_PRECONDITION = "stale_precondition"
    SUPERSEDED = "superseded"
    EXTERNAL_VERIFICATION_PENDING = "external_verification_pending"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.TERMINAL_FAILED,
        ExecutionStatus.STALE,
        ExecutionStatus.STALE_PRECONDITION,
        ExecutionStatus.SUPERSEDED,
    }
)


@dataclass(frozen=True, slots=True)
class CalendarMutation:
    action_type: CalendarActionType
    calendar_id: str
    calendar_event_id: str
    work_block_id: str
    desired_start: datetime | None
    desired_end: datetime | None
    expected_observed_event_etag: str | None
    private_properties: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class MutationResponseEvidence:
    mutation_response_etag: str | None
    mutation_response_payload_hash: str
    mutation_response_status: str
    mutation_response_received_at: datetime


@dataclass(frozen=True, slots=True)
class ActionOutbox:
    outbox_id: str
    user_id: str
    commitment_id: str
    work_block_id: str
    action_idempotency_key: str
    expected_commitment_revision: int
    expected_plan_revision: int
    expected_projection_hash: str
    expected_control_epoch: int
    before_state: Mapping[str, Any] | None
    mutation: CalendarMutation
    dispatch_status: DispatchStatus
    execution_status: ExecutionStatus
    claim_token: str | None
    claim_lease_expires_at: datetime | None
    attempts: int
    mutation_response: MutationResponseEvidence | None
    error: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime

    def _require_execution(self, *allowed: ExecutionStatus) -> None:
        if self.execution_status not in allowed:
            raise InvalidTransitionError(
                f"outbox {self.outbox_id} execution status is {self.execution_status}; "
                f"expected one of {[status.value for status in allowed]}"
            )

    def mark_queued(self, at: datetime) -> ActionOutbox:
        if self.dispatch_status != DispatchStatus.PENDING:
            raise InvalidTransitionError(
                f"outbox {self.outbox_id} dispatch status is {self.dispatch_status}; expected pending"
            )
        return replace(self, dispatch_status=DispatchStatus.QUEUED, updated_at=at)

    def mark_delivered(self, at: datetime) -> ActionOutbox:
        if self.dispatch_status not in (DispatchStatus.QUEUED, DispatchStatus.PENDING):
            raise InvalidTransitionError(
                f"outbox {self.outbox_id} dispatch status is {self.dispatch_status}; "
                "expected queued or pending"
            )
        return replace(self, dispatch_status=DispatchStatus.DELIVERED, updated_at=at)

    def claim(self, token: str, lease_expires_at: datetime, at: datetime) -> ActionOutbox:
        if self.execution_status == ExecutionStatus.CLAIMED:
            # Fenced takeover is allowed only after the previous claim lease expired.
            if self.claim_lease_expires_at is not None and self.claim_lease_expires_at > at:
                raise InvalidTransitionError(
                    f"outbox {self.outbox_id} is claimed with an unexpired lease"
                )
        else:
            self._require_execution(ExecutionStatus.PENDING, ExecutionStatus.RETRYABLE_FAILED)
        return replace(
            self,
            execution_status=ExecutionStatus.CLAIMED,
            claim_token=token,
            claim_lease_expires_at=lease_expires_at,
            updated_at=at,
        )

    def hold(self, control_epoch: int, at: datetime) -> ActionOutbox:
        self._require_execution(
            ExecutionStatus.PENDING,
            ExecutionStatus.CLAIMED,
            ExecutionStatus.RETRYABLE_FAILED,
        )
        dispatch_status = (
            DispatchStatus.HELD_BY_CONTROL
            if self.dispatch_status == DispatchStatus.PENDING
            else self.dispatch_status
        )
        return replace(
            self,
            execution_status=ExecutionStatus.HELD_BY_CONTROL,
            dispatch_status=dispatch_status,
            claim_token=None,
            claim_lease_expires_at=None,
            error={"held_at_control_epoch": control_epoch},
            updated_at=at,
        )

    def mark_stale(self, reason_code: str, at: datetime) -> ActionOutbox:
        self._require_execution(
            ExecutionStatus.PENDING,
            ExecutionStatus.CLAIMED,
            ExecutionStatus.RETRYABLE_FAILED,
            ExecutionStatus.HELD_BY_CONTROL,
        )
        return replace(
            self,
            execution_status=ExecutionStatus.STALE,
            claim_token=None,
            claim_lease_expires_at=None,
            error={"stale_reason": reason_code},
            updated_at=at,
        )

    def start_external_io(self, at: datetime) -> ActionOutbox:
        # The claimed -> action_in_flight transition is the external-I/O
        # linearization point; a pause that commits first wins.
        self._require_execution(ExecutionStatus.CLAIMED)
        return replace(
            self,
            execution_status=ExecutionStatus.ACTION_IN_FLIGHT,
            attempts=self.attempts + 1,
            updated_at=at,
        )

    def succeed(self, response: MutationResponseEvidence, at: datetime) -> ActionOutbox:
        self._require_execution(ExecutionStatus.ACTION_IN_FLIGHT)
        return replace(
            self,
            execution_status=ExecutionStatus.SUCCEEDED,
            mutation_response=response,
            claim_token=None,
            claim_lease_expires_at=None,
            error=None,
            updated_at=at,
        )

    def fail_retryably(self, error: Mapping[str, Any], at: datetime) -> ActionOutbox:
        self._require_execution(ExecutionStatus.ACTION_IN_FLIGHT, ExecutionStatus.CLAIMED)
        return replace(
            self,
            execution_status=ExecutionStatus.RETRYABLE_FAILED,
            claim_token=None,
            claim_lease_expires_at=None,
            error=dict(error),
            updated_at=at,
        )

    def fail_terminally(self, error: Mapping[str, Any], at: datetime) -> ActionOutbox:
        self._require_execution(ExecutionStatus.ACTION_IN_FLIGHT)
        return replace(
            self,
            execution_status=ExecutionStatus.TERMINAL_FAILED,
            claim_token=None,
            claim_lease_expires_at=None,
            error=dict(error),
            updated_at=at,
        )

    def fail_stale_precondition(
        self,
        error: Mapping[str, Any],
        at: datetime,
    ) -> ActionOutbox:
        # Calendar 412: the intent is stale; the only continuation is source
        # synchronization. No action_result is ever emitted for this state.
        self._require_execution(ExecutionStatus.ACTION_IN_FLIGHT)
        return replace(
            self,
            execution_status=ExecutionStatus.STALE_PRECONDITION,
            claim_token=None,
            claim_lease_expires_at=None,
            error=dict(error),
            updated_at=at,
        )

    def supersede(self, at: datetime) -> ActionOutbox:
        self._require_execution(
            ExecutionStatus.PENDING,
            ExecutionStatus.CLAIMED,
            ExecutionStatus.RETRYABLE_FAILED,
            ExecutionStatus.HELD_BY_CONTROL,
        )
        dispatch_status = (
            DispatchStatus.SUPERSEDED
            if self.dispatch_status
            in (DispatchStatus.PENDING, DispatchStatus.HELD_BY_CONTROL, DispatchStatus.QUEUED)
            else self.dispatch_status
        )
        return replace(
            self,
            execution_status=ExecutionStatus.SUPERSEDED,
            dispatch_status=dispatch_status,
            claim_token=None,
            claim_lease_expires_at=None,
            updated_at=at,
        )
