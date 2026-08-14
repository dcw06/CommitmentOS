from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.intervals import IntervalSet
from commitmentos.domain.planning.models import (
    CandidateSlot,
    CommitmentAllocation,
    CommitmentDemand,
    PlannedWorkBlock,
    PlannerInput,
    PortfolioPlan,
    TimeInterval,
)
from commitmentos.domain.planning.risk import RiskCalculator, RiskInput
from commitmentos.domain.planning.scoring import SlotScorer
from commitmentos.domain.shared.types import CanonicalEncoder

CONSTRAINT_VERSION = "hard-constraints-v1"


class PortfolioPlanner:
    def __init__(
        self,
        slot_generator: CandidateSlotGenerator,
        constraint_evaluator: ConstraintEvaluator,
        slot_scorer: SlotScorer,
        risk_calculator: RiskCalculator | None = None,
    ) -> None:
        self._slot_generator = slot_generator
        self._constraint_evaluator = constraint_evaluator
        self._slot_scorer = slot_scorer
        self._risk_calculator = risk_calculator or RiskCalculator("portfolio-risk-v1")

    def plan(self, planner_input: PlannerInput) -> PortfolioPlan:
        order = self.order_commitments(planner_input)
        demand_by_id = {item.commitment_id: item for item in planner_input.demands}
        preserved = sorted(
            planner_input.preserved_blocks,
            key=lambda item: (
                item.interval.start.astimezone(timezone.utc),
                item.interval.end.astimezone(timezone.utc),
                item.work_block_id,
            ),
        )
        free = self._slot_generator.free_capacity(
            planner_input.planning_horizon,
            planner_input.busy_intervals,
            preserved,
            planner_input.preferences,
        )
        occupied: list[TimeInterval] = [item.interval for item in planner_input.busy_intervals]
        occupied.extend(item.interval for item in preserved)
        daily_focus: list[TimeInterval] = [item.interval for item in preserved]
        planned: list[PlannedWorkBlock] = [
            PlannedWorkBlock(
                work_block_id=item.work_block_id,
                commitment_id=item.commitment_id,
                interval=item.interval,
                duration_minutes=item.duration_minutes,
                preserved=True,
                calendar_event_id=item.calendar_event_id,
            )
            for item in preserved
        ]
        newly_allocated: dict[str, int] = defaultdict(int)
        used_ids = set(planner_input.known_work_block_ids)
        used_ids.update(item.work_block_id for item in preserved)

        for commitment_id in order:
            demand = demand_by_id[commitment_id]
            preserved_minutes = sum(
                item.duration_minutes
                for item in preserved
                if item.commitment_id == commitment_id
            )
            deficit = max(demand.remaining_minutes - preserved_minutes, 0)
            ordinal = 0
            while deficit >= planner_input.preferences.minimum_block_minutes:
                candidate_demand = replace(demand, remaining_minutes=deficit)
                candidates = self._slot_generator.generate(
                    free,
                    candidate_demand,
                    planner_input.preferences,
                    planner_input.planning_horizon.start,
                )
                eligible = [
                    candidate
                    for candidate in candidates
                    if self._constraint_evaluator.is_valid(
                        candidate.interval,
                        candidate_demand,
                        planner_input.preferences,
                        occupied,
                        planner_input.planning_horizon.start,
                        daily_focus,
                    )
                ]
                if not eligible:
                    break
                chosen = min(
                    eligible,
                    key=lambda candidate: self._candidate_rank(
                        candidate,
                        candidate_demand,
                        planner_input,
                        daily_focus,
                    ),
                )
                while True:
                    work_block_id = CanonicalEncoder.hash(
                        ["work-block:v1", commitment_id, ordinal]
                    )
                    ordinal += 1
                    if work_block_id not in used_ids:
                        break
                used_ids.add(work_block_id)
                duration = chosen.interval.duration_minutes()
                planned.append(
                    PlannedWorkBlock(
                        work_block_id=work_block_id,
                        commitment_id=commitment_id,
                        interval=chosen.interval,
                        duration_minutes=duration,
                        preserved=False,
                    )
                )
                occupied.append(chosen.interval)
                daily_focus.append(chosen.interval)
                newly_allocated[commitment_id] += duration
                deficit -= duration

        new_intervals = [item.interval for item in planned if not item.preserved]
        unallocated = IntervalSet(free).subtract(new_intervals)
        allocations: list[CommitmentAllocation] = []
        risk_audit: dict[str, dict[str, int | float | str | None]] = {}
        for commitment_id in order:
            demand = demand_by_id[commitment_id]
            commitment_blocks = [
                block for block in planned if block.commitment_id == commitment_id
            ]
            preserved_minutes = sum(
                block.duration_minutes for block in commitment_blocks if block.preserved
            )
            allocation_deficit = max(
                demand.remaining_minutes - preserved_minutes,
                0,
            )
            new_minutes = newly_allocated[commitment_id]
            allocated = preserved_minutes + new_minutes
            shortfall = max(demand.remaining_minutes - allocated, 0)
            slack = self._slack_before(unallocated, demand.deadline)
            projected_finish = max(
                (block.interval.end for block in commitment_blocks),
                key=lambda value: value.astimezone(timezone.utc),
                default=None,
            )
            risk_input = RiskInput(
                lifecycle_status=demand.lifecycle_status,
                deadline_passed=(
                    demand.deadline.astimezone(timezone.utc)
                    <= planner_input.planning_horizon.start.astimezone(timezone.utc)
                ),
                effort_confirmed=demand.effort_confirmed,
                remaining_minutes=demand.remaining_minutes,
                allocated_work_minutes=allocated,
                shortfall_minutes=shortfall,
                portfolio_slack_minutes=slack,
            )
            risk_level = self._risk_calculator.calculate(risk_input)
            allocations.append(
                CommitmentAllocation(
                    commitment_id=commitment_id,
                    preserved_reserved_minutes=preserved_minutes,
                    allocation_deficit=allocation_deficit,
                    newly_allocated_minutes=new_minutes,
                    allocated_work_minutes=allocated,
                    shortfall_minutes=shortfall,
                    portfolio_slack_minutes=slack,
                    projected_finish=projected_finish,
                    risk_level=risk_level,
                )
            )
            risk_audit[commitment_id] = {
                "commitment_revision": demand.commitment_revision,
                "confirmed_effort": demand.confirmed_effort_minutes,
                "verified_completed_minutes": demand.verified_completed_minutes,
                "remaining_minutes": demand.remaining_minutes,
                "preserved_reserved_minutes": preserved_minutes,
                "allocation_deficit": allocation_deficit,
                "newly_allocated_minutes": new_minutes,
                "allocated_work_minutes": allocated,
                "shortfall_minutes": shortfall,
                "portfolio_slack_minutes": slack,
                "slack_ratio": self._risk_calculator.slack_ratio(
                    demand.remaining_minutes, slack
                ),
                "previous_risk": demand.previous_risk_level.value,
                "new_risk": risk_level.value,
                "threshold_version": self._risk_calculator.calculator_version,
            }

        planned_tuple = tuple(
            sorted(
                planned,
                key=lambda item: (
                    item.interval.start.astimezone(timezone.utc),
                    item.interval.end.astimezone(timezone.utc),
                    item.commitment_id,
                    item.work_block_id,
                ),
            )
        )
        planner_run_id = self._planner_run_id(planner_input, order, planned_tuple)
        result = PortfolioPlan(
            planner_run_id=planner_run_id,
            planner_version=planner_input.planner_version,
            calendar_state_revision=planner_input.calendar_state_revision,
            calendar_snapshot_hash=planner_input.calendar_snapshot_hash,
            expected_revisions=dict(planner_input.expected_revisions),
            commitment_order=order,
            work_blocks=planned_tuple,
            allocations=tuple(allocations),
            unallocated_intervals=unallocated,
            feasible=all(item.shortfall_minutes == 0 for item in allocations),
            user_id=planner_input.user_id,
            constraint_version=CONSTRAINT_VERSION,
            score_version=self._slot_scorer.score_version,
            threshold_version=self._risk_calculator.calculator_version,
            calculated_at=planner_input.planning_horizon.start,
            planning_horizon=planner_input.planning_horizon,
            risk_audit=risk_audit,
        )
        if not self.validate_single_allocation(result):
            raise ValueError("portfolio planner allocated the same minute more than once")
        return result

    def order_commitments(self, planner_input: PlannerInput) -> tuple[str, ...]:
        return tuple(
            item.commitment_id
            for item in sorted(
                planner_input.demands,
                key=lambda item: (
                    item.deadline.astimezone(timezone.utc),
                    item.explicit_priority,
                    item.created_at.astimezone(timezone.utc),
                    item.commitment_id,
                ),
            )
        )

    def validate_single_allocation(self, plan: PortfolioPlan) -> bool:
        ordered = sorted(
            plan.work_blocks,
            key=lambda item: item.interval.start.astimezone(timezone.utc),
        )
        return all(
            not left.interval.overlaps(right.interval)
            for index, left in enumerate(ordered)
            for right in ordered[index + 1 :]
        )

    def _candidate_rank(
        self,
        candidate: CandidateSlot,
        demand: CommitmentDemand,
        planner_input: PlannerInput,
        daily_focus: Sequence[TimeInterval],
    ) -> tuple[int, int, int, datetime, datetime]:
        zone = ZoneInfo(planner_input.preferences.timezone)
        local_day = candidate.interval.start.astimezone(zone).date()
        daily_load = sum(
            interval.duration_minutes()
            for interval in daily_focus
            if interval.start.astimezone(zone).date() == local_day
        )
        back_to_back = int(
            any(
                interval.end.astimezone(timezone.utc)
                == candidate.interval.start.astimezone(timezone.utc)
                or candidate.interval.end.astimezone(timezone.utc)
                == interval.start.astimezone(timezone.utc)
                for interval in daily_focus
            )
        )
        score, _ = self._slot_scorer.score(
            candidate.interval,
            demand,
            planner_input.preferences,
        )
        return (
            daily_load,
            back_to_back,
            -score,
            candidate.interval.start.astimezone(timezone.utc),
            candidate.interval.end.astimezone(timezone.utc),
        )

    @staticmethod
    def _slack_before(intervals: Sequence[TimeInterval], deadline: datetime) -> int:
        deadline_utc = deadline.astimezone(timezone.utc)
        minutes = 0
        for interval in intervals:
            start = interval.start.astimezone(timezone.utc)
            end = min(interval.end.astimezone(timezone.utc), deadline_utc)
            if start < end:
                minutes += int((end - start).total_seconds() // 60)
        return minutes

    def _planner_run_id(
        self,
        planner_input: PlannerInput,
        order: tuple[str, ...],
        blocks: tuple[PlannedWorkBlock, ...],
    ) -> str:
        parts: list[str | int | datetime | None] = [
            "planner-run:v1",
            planner_input.user_id,
            planner_input.planner_version,
            self._slot_scorer.score_version,
            self._risk_calculator.calculator_version,
            planner_input.calendar_state_revision,
            planner_input.calendar_snapshot_hash,
            planner_input.planning_horizon.start,
            planner_input.planning_horizon.end,
        ]
        for key, revision in sorted(planner_input.expected_revisions.items()):
            parts.extend([key, revision])
        parts.extend(order)
        for block in blocks:
            parts.extend(
                [
                    block.work_block_id,
                    block.commitment_id,
                    block.interval.start,
                    block.interval.end,
                    int(block.preserved),
                ]
            )
        return CanonicalEncoder.hash(parts)
