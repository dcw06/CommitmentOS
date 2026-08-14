from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Mapping
from zoneinfo import ZoneInfo

from commitmentos.domain.commitments.models import LifecycleStatus, RiskLevel


@dataclass(frozen=True, slots=True, order=True)
class TimeInterval:
    """Half-open interval [start, end); boundary-touching intervals do not overlap."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("interval start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("interval end must be timezone-aware")
        if self._instant(self.end) <= self._instant(self.start):
            raise ValueError("interval end must be after start")

    @staticmethod
    def _instant(value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    def duration_minutes(self) -> int:
        return int((self._instant(self.end) - self._instant(self.start)).total_seconds() // 60)

    def overlaps(self, other: TimeInterval) -> bool:
        return (
            self._instant(self.start) < self._instant(other.end)
            and self._instant(other.start) < self._instant(self.end)
        )

    def contains(self, other: TimeInterval) -> bool:
        return (
            self._instant(self.start) <= self._instant(other.start)
            and self._instant(other.end) <= self._instant(self.end)
        )


@dataclass(frozen=True, slots=True)
class UserPlanningPreferences:
    timezone: str
    working_day_start: time
    working_day_end: time
    minimum_block_minutes: int
    maximum_block_minutes: int
    daily_focus_limit_minutes: int
    preferred_focus_windows: tuple[tuple[time, time], ...]

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if self.working_day_end <= self.working_day_start:
            raise ValueError("working day end must be after its start")
        if self.minimum_block_minutes <= 0:
            raise ValueError("minimum block minutes must be positive")
        if self.maximum_block_minutes < self.minimum_block_minutes:
            raise ValueError("maximum block minutes must be at least the minimum")
        if self.daily_focus_limit_minutes < self.minimum_block_minutes:
            raise ValueError("daily focus limit must allow at least one minimum block")
        for start, end in self.preferred_focus_windows:
            if end <= start:
                raise ValueError("preferred focus-window end must be after its start")
            if start < self.working_day_start or end > self.working_day_end:
                raise ValueError("preferred focus windows must stay inside working hours")


@dataclass(frozen=True, slots=True)
class CommitmentDemand:
    commitment_id: str
    commitment_revision: int
    plan_revision: int
    deadline: datetime
    remaining_minutes: int
    explicit_priority: int
    created_at: datetime
    confirmed_effort_minutes: int | None = None
    verified_completed_minutes: int = 0
    effort_confirmed: bool = True
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
    previous_risk_level: RiskLevel = RiskLevel.UNKNOWN


@dataclass(frozen=True, slots=True)
class CalendarBusyInterval:
    snapshot_id: str
    calendar_event_id: str
    interval: TimeInterval
    is_app_owned: bool
    work_block_id: str | None
    source_revision: int


@dataclass(frozen=True, slots=True)
class PreservedWorkBlock:
    work_block_id: str
    commitment_id: str
    interval: TimeInterval
    duration_minutes: int
    plan_revision: int
    calendar_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerInput:
    user_id: str
    planning_horizon: TimeInterval
    preferences: UserPlanningPreferences
    demands: tuple[CommitmentDemand, ...]
    busy_intervals: tuple[CalendarBusyInterval, ...]
    preserved_blocks: tuple[PreservedWorkBlock, ...]
    expected_revisions: Mapping[str, int]
    calendar_state_revision: int
    calendar_snapshot_hash: str
    planner_version: str
    known_work_block_ids: tuple[str, ...] = ()
    immutable_work_block_ids: tuple[str, ...] = ()
    adopted_work_block_ids: tuple[str, ...] = ()
    affected_work_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateSlot:
    interval: TimeInterval
    score: int
    score_components: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PlannedWorkBlock:
    work_block_id: str
    commitment_id: str
    interval: TimeInterval
    duration_minutes: int
    preserved: bool
    calendar_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommitmentAllocation:
    commitment_id: str
    preserved_reserved_minutes: int
    allocation_deficit: int
    newly_allocated_minutes: int
    allocated_work_minutes: int
    shortfall_minutes: int
    portfolio_slack_minutes: int
    projected_finish: datetime | None
    risk_level: RiskLevel


class PlannerRunStatus(StrEnum):
    CALCULATED = "calculated"
    PUBLISHED = "published"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    planner_run_id: str
    planner_version: str
    calendar_state_revision: int
    calendar_snapshot_hash: str
    expected_revisions: Mapping[str, int]
    commitment_order: tuple[str, ...]
    work_blocks: tuple[PlannedWorkBlock, ...]
    allocations: tuple[CommitmentAllocation, ...]
    unallocated_intervals: tuple[TimeInterval, ...]
    feasible: bool
    user_id: str
    constraint_version: str
    score_version: str
    threshold_version: str
    calculated_at: datetime
    planning_horizon: TimeInterval
    risk_audit: Mapping[str, Mapping[str, int | float | str | None]]
    status: PlannerRunStatus = PlannerRunStatus.CALCULATED
    stale_reason: str | None = None
    published_at: datetime | None = None


class PlanMutationType(StrEnum):
    CREATE = "create"
    MOVE = "move"
    ADOPT = "adopt"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class PlanMutation:
    mutation_type: PlanMutationType
    work_block_id: str
    commitment_id: str
    before: TimeInterval | None
    after: TimeInterval | None
    reason: str
    calendar_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanDiff:
    mutations: tuple[PlanMutation, ...]
    preserved_work_block_ids: tuple[str, ...]
    moved_block_count: int
    total_displacement_minutes: int
    metadata: Mapping[str, str]
