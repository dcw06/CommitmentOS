from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    HELD = "held"
    NO_OP = "no_op"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class CommandResult:
    status: CommandStatus
    identifiers: Mapping[str, str]
    revision: int | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class Page:
    items: Sequence[Mapping[str, Any]]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    user_id: str
    email: str
    session_id: str
    authenticated_at: datetime


@dataclass(frozen=True, slots=True)
class ControlChangeRequest:
    control_name: str
    target_mode: str
    reason: str
    expected_control_epoch: int


@dataclass(frozen=True, slots=True)
class FencedLease:
    lease_key: str
    owner: str
    fencing_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationClaim:
    observation_id: str
    user_id: str
    run_id: str
    dispatch_generation: int
    claimed_control_epoch: int
    processing_fence: FencedLease


@dataclass(frozen=True, slots=True)
class ReconciliationRequest:
    run_id: str
    observation_id: str
    user_id: str
    trace_id: str
    workflow_version: str
    dispatch_generation: int
    claimed_control_epoch: int
    processing_fence: FencedLease


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    run_id: str
    observation_id: str
    status: str
    durable_outcome_ids: tuple[str, ...]
    error_code: str | None
    retryable: bool
    processing_fencing_token: str
