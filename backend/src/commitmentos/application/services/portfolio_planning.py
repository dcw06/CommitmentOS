from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.planning_inputs import PlanningInputReader
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.commitments.models import Commitment, RiskLevel
from commitmentos.domain.planning.calendar_state import (
    CalendarSnapshotReducer,
    CalendarStateSnapshot,
)
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CommitmentDemand,
    PlannerInput,
    PortfolioPlan,
    PreservedWorkBlock,
    TimeInterval,
    UserPlanningPreferences,
)
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.repair import StablePlanRepairer
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)
from commitmentos.domain.progress.service import ProgressCalculator
from commitmentos.domain.shared.errors import InvalidTransitionError

PLANNER_VERSION = "portfolio-greedy-v1"


@dataclass(frozen=True, slots=True)
class PortfolioFacts:
    commitments: tuple[Commitment, ...]
    work_blocks: tuple[WorkBlock, ...]
    preferences_revision: int


class PortfolioPlanningService:
    """Assemble authoritative inputs and run the pure portfolio planner.

    Calendar inputs come only from a consistently published snapshot. The
    cursor revision and snapshot hash join every other expected revision in
    the publication guard.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        planning_inputs: PlanningInputReader,
        planner: PortfolioPlanner,
        clock: Clock,
        constraint_evaluator: ConstraintEvaluator | None = None,
        planner_version: str = PLANNER_VERSION,
        repairer: StablePlanRepairer | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._planning_inputs = planning_inputs
        self._planner = planner
        self._clock = clock
        self._constraints = constraint_evaluator or ConstraintEvaluator()
        self._progress = ProgressCalculator()
        self._calendar_reducer = CalendarSnapshotReducer()
        self._planner_version = planner_version
        self._repairer = repairer or StablePlanRepairer(
            constraint_evaluator=self._constraints,
            planner=self._planner,
        )

    async def calculate(
        self,
        user_id: str,
        include_commitment_ids: Sequence[str] = (),
    ) -> PortfolioPlan:
        preferences = await self._planning_inputs.get_preferences(user_id)
        facts = await self._load_facts(user_id, include_commitment_ids)
        now = self._clock.now()
        horizon = self._planning_horizon(facts.commitments, now)
        calendar_state = await self._planning_inputs.load_calendar_state(user_id, horizon)
        busy = self._calendar_reducer.busy_intervals(calendar_state.events, horizon)
        planner_input = self._planner_input(
            user_id,
            horizon,
            preferences,
            facts,
            busy,
            calendar_state,
        )
        return self._planner.plan(planner_input)

    async def calculate_repair(
        self,
        user_id: str,
        previous_plan: PortfolioPlan,
        include_commitment_ids: Sequence[str] = (),
    ) -> tuple[PortfolioPlan, UserPlanningPreferences]:
        """Repair from one consistently published Calendar snapshot."""
        preferences = await self._planning_inputs.get_preferences(user_id)
        facts = await self._load_facts(user_id, include_commitment_ids)
        now = self._clock.now()
        horizon = self._planning_horizon(facts.commitments, now)
        calendar_state = await self._planning_inputs.load_calendar_state(user_id, horizon)
        busy = self._calendar_reducer.busy_intervals(calendar_state.events, horizon)
        planner_input = self._planner_input(
            user_id,
            horizon,
            preferences,
            facts,
            busy,
            calendar_state,
        )
        return self._repairer.repair(planner_input, previous_plan), preferences

    async def calendar_inputs_match(self, plan: PortfolioPlan) -> bool:
        try:
            current = await self._planning_inputs.load_calendar_state(
                plan.user_id, plan.planning_horizon
            )
        except InvalidTransitionError:
            return False
        return (
            current.calendar_state_revision == plan.calendar_state_revision
            and current.calendar_snapshot_hash == plan.calendar_snapshot_hash
        )

    async def expected_revisions_match(
        self,
        repositories: RepositorySet,
        plan: PortfolioPlan,
    ) -> bool:
        expected_commitment_ids = {
            key.partition(":")[2]
            for key in plan.expected_revisions
            if key.startswith("commitment:")
        }
        expected_work_block_ids = {
            key.partition(":")[2]
            for key in plan.expected_revisions
            if key.startswith("work_block:")
        }
        current_active_ids = {
            item.commitment_id
            for item in await repositories.commitments.list_active(plan.user_id)
        }
        if not current_active_ids.issubset(expected_commitment_ids):
            return False
        for key, expected in plan.expected_revisions.items():
            kind, _, identifier = key.partition(":")
            if kind == "commitment":
                current = await repositories.commitments.get(identifier)
                if current is None or current.revision != expected:
                    return False
            elif kind == "work_block":
                current_block = await repositories.work_blocks.get(identifier)
                if current_block is None or current_block.revision != expected:
                    return False
            elif kind == "preferences":
                user = await repositories.users.get(identifier)
                if user is None or int(user.get("planning_preferences_revision", 0)) != expected:
                    return False
            elif kind == "calendar":
                cursor = await repositories.sync_cursors.get(
                    identifier, SourceType.CALENDAR
                )
                if (
                    cursor is None
                    or cursor.calendar_state_revision != expected
                    or cursor.publish_in_progress_generation_id is not None
                    or cursor.full_resync_required
                ):
                    return False
            else:
                return False
        current_work_block_ids: set[str] = set()
        for commitment_id in expected_commitment_ids:
            current_work_block_ids.update(
                item.work_block_id
                for item in await repositories.work_blocks.list_for_commitment(
                    commitment_id
                )
            )
        if current_work_block_ids != expected_work_block_ids:
            return False
        return True

    async def _load_facts(
        self,
        user_id: str,
        include_commitment_ids: Sequence[str],
    ) -> PortfolioFacts:
        async def _load(repositories: RepositorySet) -> PortfolioFacts:
            active = list(await repositories.commitments.list_active(user_id))
            by_id = {item.commitment_id: item for item in active}
            for commitment_id in include_commitment_ids:
                commitment = await repositories.commitments.get(commitment_id)
                if commitment is not None and commitment.user_id == user_id:
                    by_id[commitment_id] = commitment
            commitments = tuple(by_id[key] for key in sorted(by_id))
            blocks: list[WorkBlock] = []
            for commitment in commitments:
                blocks.extend(
                    await repositories.work_blocks.list_for_commitment(
                        commitment.commitment_id
                    )
                )
            user = await repositories.users.get(user_id)
            return PortfolioFacts(
                commitments=commitments,
                work_blocks=tuple(
                    sorted(blocks, key=lambda item: item.work_block_id)
                ),
                preferences_revision=int(
                    (user or {}).get("planning_preferences_revision", 0)
                ),
            )

        return await self._unit_of_work.read(_load)

    @staticmethod
    def _planning_horizon(
        commitments: Sequence[Commitment],
        now: datetime,
    ) -> TimeInterval:
        latest_deadline = max(
            (
                commitment.deadline.value.astimezone(timezone.utc)
                for commitment in commitments
            ),
            default=now.astimezone(timezone.utc) + timedelta(days=1),
        )
        end = max(latest_deadline, now.astimezone(timezone.utc) + timedelta(minutes=15))
        return TimeInterval(now, end)

    def _planner_input(
        self,
        user_id: str,
        horizon: TimeInterval,
        preferences: UserPlanningPreferences,
        facts: PortfolioFacts,
        busy: Sequence[CalendarBusyInterval],
        calendar_state: CalendarStateSnapshot,
    ) -> PlannerInput:
        commitments = {item.commitment_id: item for item in facts.commitments}
        blocks_by_commitment: dict[str, list[WorkBlock]] = {
            commitment_id: [] for commitment_id in commitments
        }
        for block in facts.work_blocks:
            blocks_by_commitment.setdefault(block.commitment_id, []).append(block)
        preserved = self._preserved_blocks(
            facts.commitments,
            facts.work_blocks,
            busy,
            preferences,
            horizon.start,
        )
        expected: dict[str, int] = {
            f"preferences:{user_id}": facts.preferences_revision,
            f"calendar:{user_id}": calendar_state.calendar_state_revision,
        }
        for commitment in facts.commitments:
            expected[f"commitment:{commitment.commitment_id}"] = commitment.revision
        for block in facts.work_blocks:
            expected[f"work_block:{block.work_block_id}"] = block.revision
        demands: list[CommitmentDemand] = []
        for commitment in facts.commitments:
            work_blocks = blocks_by_commitment.get(commitment.commitment_id, [])
            effort_confirmed = commitment.effort.confirmed_minutes is not None
            remaining = (
                self._progress.active_remaining_minutes(commitment, work_blocks)
                if effort_confirmed
                else 0
            )
            demands.append(
                CommitmentDemand(
                    commitment_id=commitment.commitment_id,
                    commitment_revision=commitment.revision,
                    plan_revision=commitment.plan_revision,
                    deadline=commitment.deadline.value,
                    remaining_minutes=remaining,
                    explicit_priority=self._priority(commitment),
                    created_at=commitment.created_at,
                    confirmed_effort_minutes=commitment.effort.confirmed_minutes,
                    verified_completed_minutes=self._progress.verified_minutes(
                        work_blocks
                    ),
                    effort_confirmed=effort_confirmed,
                    lifecycle_status=commitment.lifecycle_status,
                    previous_risk_level=(
                        commitment.projection.risk_level
                        if commitment.projection is not None
                        else RiskLevel.UNKNOWN
                    ),
                )
            )
        preserved_ids = {item.work_block_id for item in preserved}
        return PlannerInput(
            user_id=user_id,
            planning_horizon=horizon,
            preferences=preferences,
            demands=tuple(demands),
            busy_intervals=tuple(busy),
            preserved_blocks=preserved,
            expected_revisions=expected,
            calendar_state_revision=calendar_state.calendar_state_revision,
            calendar_snapshot_hash=calendar_state.calendar_snapshot_hash,
            planner_version=self._planner_version,
            known_work_block_ids=tuple(
                sorted(block.work_block_id for block in facts.work_blocks)
            ),
            immutable_work_block_ids=tuple(
                sorted(
                    block.work_block_id
                    for block in facts.work_blocks
                    if block.execution_state
                    in (
                        WorkBlockExecutionState.ACTIVE,
                        WorkBlockExecutionState.COMPLETED,
                    )
                )
            ),
            adopted_work_block_ids=tuple(
                sorted(
                    block.work_block_id
                    for block in facts.work_blocks
                    if block.user_edit_state == UserEditState.ADOPTED
                )
            ),
            affected_work_block_ids=tuple(
                sorted(
                    block.work_block_id
                    for block in facts.work_blocks
                    if block.execution_state
                    in (
                        WorkBlockExecutionState.PLANNED,
                        WorkBlockExecutionState.ACTIVE,
                    )
                    and block.work_block_id not in preserved_ids
                )
            ),
        )

    def _preserved_blocks(
        self,
        commitments: Sequence[Commitment],
        work_blocks: Sequence[WorkBlock],
        busy: Sequence[CalendarBusyInterval],
        preferences: UserPlanningPreferences,
        now: datetime,
    ) -> tuple[PreservedWorkBlock, ...]:
        commitment_by_id = {item.commitment_id: item for item in commitments}
        preserved: list[PreservedWorkBlock] = []
        daily_focus: list[TimeInterval] = []
        ordered = sorted(
            work_blocks,
            key=lambda item: (
                item.scheduled_start.astimezone(timezone.utc),
                item.work_block_id,
            ),
        )
        for block in ordered:
            commitment = commitment_by_id.get(block.commitment_id)
            if commitment is None:
                continue
            if block.execution_state not in (
                WorkBlockExecutionState.PLANNED,
                WorkBlockExecutionState.ACTIVE,
            ):
                continue
            if block.user_edit_state in (
                UserEditState.INVALID_MOVE,
                UserEditState.USER_DELETED,
            ):
                continue
            interval = TimeInterval(block.scheduled_start, block.scheduled_end)
            if interval.end.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                continue
            occupied = [
                item.interval
                for item in busy
                if item.work_block_id != block.work_block_id
            ]
            occupied.extend(item.interval for item in preserved)
            demand = CommitmentDemand(
                commitment_id=commitment.commitment_id,
                commitment_revision=commitment.revision,
                plan_revision=commitment.plan_revision,
                deadline=commitment.deadline.value,
                remaining_minutes=block.duration_minutes,
                explicit_priority=0,
                created_at=commitment.created_at,
            )
            # A currently active block is immutable even though its start is
            # now in the past. Future planned blocks must satisfy every hard
            # constraint to count as preserved capacity.
            valid = block.execution_state == WorkBlockExecutionState.ACTIVE or (
                self._constraints.is_valid(
                    interval,
                    demand,
                    preferences,
                    occupied,
                    now,
                    daily_focus,
                )
            )
            if not valid:
                continue
            preserved.append(
                PreservedWorkBlock(
                    work_block_id=block.work_block_id,
                    commitment_id=block.commitment_id,
                    interval=interval,
                    duration_minutes=block.duration_minutes,
                    plan_revision=block.plan_revision,
                    calendar_event_id=block.calendar_event_id,
                )
            )
            daily_focus.append(interval)
        return tuple(preserved)

    @staticmethod
    def _priority(commitment: Commitment) -> int:
        try:
            return int(commitment.owner.get("priority", "0"))
        except (TypeError, ValueError):
            return 0
