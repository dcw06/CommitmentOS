from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from commitmentos.domain.shared.errors import InvalidTransitionError


class OwnershipType(StrEnum):
    MY_COMMITMENT = "my_commitment"
    REQUEST_TO_ME = "request_to_me"
    COMMITMENT_TO_ME = "commitment_to_me"
    AMBIGUOUS = "ambiguous"


class LifecycleStatus(StrEnum):
    CANDIDATE = "candidate"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    COMPLETION_CANDIDATE = "completion_candidate"
    COMPLETED = "completed"
    PAUSED = "paused"
    DISMISSED = "dismissed"
    CANCELED = "canceled"


class RiskLevel(StrEnum):
    UNKNOWN = "unknown"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    CRITICAL = "critical"
    OVERDUE = "overdue"


class BlockingStatus(StrEnum):
    CLEAR = "clear"
    WAITING = "waiting"
    BLOCKED = "blocked"


_ALLOWED_TRANSITIONS: Mapping[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.CANDIDATE: frozenset(
        {
            LifecycleStatus.AWAITING_CONFIRMATION,
            LifecycleStatus.DISMISSED,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.AWAITING_CONFIRMATION: frozenset(
        {
            LifecycleStatus.ACTIVE,
            LifecycleStatus.DISMISSED,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.ACTIVE: frozenset(
        {
            LifecycleStatus.IN_PROGRESS,
            LifecycleStatus.COMPLETION_CANDIDATE,
            LifecycleStatus.PAUSED,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.IN_PROGRESS: frozenset(
        {
            LifecycleStatus.COMPLETION_CANDIDATE,
            LifecycleStatus.PAUSED,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.COMPLETION_CANDIDATE: frozenset(
        {
            LifecycleStatus.ACTIVE,
            LifecycleStatus.IN_PROGRESS,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.PAUSED: frozenset(
        {
            LifecycleStatus.ACTIVE,
            LifecycleStatus.IN_PROGRESS,
            LifecycleStatus.DISMISSED,
            LifecycleStatus.CANCELED,
        }
    ),
    LifecycleStatus.COMPLETED: frozenset(),
    LifecycleStatus.DISMISSED: frozenset(),
    LifecycleStatus.CANCELED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Deadline:
    value: datetime
    timezone: str
    confidence: float
    evidence_id: str
    source_expression: str
    rule_version: str


@dataclass(frozen=True, slots=True)
class Effort:
    proposed_minutes: int | None
    confidence: float
    confirmed_minutes: int | None
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CommitmentProjection:
    verified_completed_minutes: int
    remaining_minutes: int
    risk_level: RiskLevel
    blocking_status: BlockingStatus
    source_commitment_revision: int
    source_work_block_revision_hash: str
    planner_run_id: str
    calculator_version: str
    computed_at: datetime

    def matches(self, commitment_revision: int, work_block_revision_hash: str) -> bool:
        return (
            self.source_commitment_revision == commitment_revision
            and self.source_work_block_revision_hash == work_block_revision_hash
        )


@dataclass(frozen=True, slots=True)
class Commitment:
    commitment_id: str
    user_id: str
    revision: int
    source_thread_id: str
    semantic_fingerprint: str
    title: str
    description: str
    ownership_type: OwnershipType
    owner: Mapping[str, str]
    beneficiary: Mapping[str, str]
    deadline: Deadline
    effort: Effort
    lifecycle_status: LifecycleStatus
    completion_evidence_id: str | None
    completed_at: datetime | None
    plan_revision: int
    projection: CommitmentProjection | None
    policy_profile: str
    created_at: datetime
    updated_at: datetime

    def transition(self, target: LifecycleStatus, at: datetime) -> Commitment:
        if target == self.lifecycle_status:
            return self
        if target == LifecycleStatus.COMPLETED:
            raise InvalidTransitionError(
                "completion requires explicit evidence; use complete() instead of transition()"
            )
        allowed = _ALLOWED_TRANSITIONS.get(self.lifecycle_status, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(
                f"lifecycle transition {self.lifecycle_status} -> {target} is not allowed"
            )
        return replace(
            self,
            lifecycle_status=target,
            revision=self.revision + 1,
            updated_at=at,
        )

    def confirm_effort(self, minutes: int, at: datetime) -> Commitment:
        if minutes <= 0:
            raise InvalidTransitionError("confirmed effort must be a positive number of minutes")
        if self.lifecycle_status == LifecycleStatus.COMPLETED:
            raise InvalidTransitionError("cannot confirm effort on a completed commitment")
        return replace(
            self,
            effort=replace(self.effort, confirmed_minutes=minutes, confirmed_at=at),
            revision=self.revision + 1,
            updated_at=at,
        )

    def complete(self, evidence_id: str, at: datetime) -> Commitment:
        if self.lifecycle_status == LifecycleStatus.COMPLETED:
            raise InvalidTransitionError("commitment is already completed")
        if self.lifecycle_status in (LifecycleStatus.DISMISSED, LifecycleStatus.CANCELED):
            raise InvalidTransitionError(
                f"cannot complete a commitment in terminal state {self.lifecycle_status}"
            )
        if not evidence_id:
            raise InvalidTransitionError("completion requires a completion evidence reference")
        return replace(
            self,
            lifecycle_status=LifecycleStatus.COMPLETED,
            completion_evidence_id=evidence_id,
            completed_at=at,
            revision=self.revision + 1,
            updated_at=at,
        )

    def reopen(self, at: datetime) -> Commitment:
        # Reopening is an explicit user action creating a new revision; reconciliation
        # must never call this (plan §4.5 completion invariant).
        if self.lifecycle_status != LifecycleStatus.COMPLETED:
            raise InvalidTransitionError("only a completed commitment can be reopened")
        return replace(
            self,
            lifecycle_status=LifecycleStatus.ACTIVE,
            completion_evidence_id=None,
            completed_at=None,
            revision=self.revision + 1,
            updated_at=at,
        )

    def with_projection(self, projection: CommitmentProjection, at: datetime) -> Commitment:
        # Projections are replaceable read state carrying their own provenance;
        # attaching one never advances the authoritative fact revision.
        if projection.source_commitment_revision != self.revision:
            raise InvalidTransitionError(
                "projection provenance does not match the current commitment revision"
            )
        return replace(self, projection=projection, updated_at=at)
