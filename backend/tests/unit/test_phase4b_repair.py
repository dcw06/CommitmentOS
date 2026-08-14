from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.diff import PlanDiffer
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CommitmentDemand,
    PlannerInput,
    PreservedWorkBlock,
    TimeInterval,
    UserPlanningPreferences,
)
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.repair import StablePlanRepairer
from commitmentos.domain.planning.repair_policy import (
    RepairPolicyDisposition,
    RepairPolicyEvaluator,
)
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.planning.scoring import SlotScorer

NOW = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)


def preferences() -> UserPlanningPreferences:
    return UserPlanningPreferences(
        timezone="UTC",
        working_day_start=time(9),
        working_day_end=time(17, 30),
        minimum_block_minutes=60,
        maximum_block_minutes=60,
        daily_focus_limit_minutes=240,
        preferred_focus_windows=((time(9), time(17, 30)),),
    )


def demand() -> CommitmentDemand:
    return CommitmentDemand(
        commitment_id="commitment-repair",
        commitment_revision=4,
        plan_revision=2,
        deadline=NOW + timedelta(hours=8),
        remaining_minutes=120,
        explicit_priority=0,
        created_at=NOW - timedelta(days=1),
        confirmed_effort_minutes=120,
    )


def planner() -> PortfolioPlanner:
    constraints = ConstraintEvaluator()
    return PortfolioPlanner(
        CandidateSlotGenerator(15),
        constraints,
        SlotScorer("stable-slot-score-v1"),
        RiskCalculator("portfolio-risk-v1"),
    )


def base_input() -> PlannerInput:
    return PlannerInput(
        user_id="user-repair",
        planning_horizon=TimeInterval(NOW, NOW + timedelta(hours=8)),
        preferences=preferences(),
        demands=(demand(),),
        busy_intervals=(),
        preserved_blocks=(),
        expected_revisions={"commitment:commitment-repair": 4},
        calendar_state_revision=1,
        calendar_snapshot_hash="before",
        planner_version="portfolio-greedy-v1",
    )


def test_stable_repair_moves_only_affected_block_by_minimum_displacement() -> None:
    previous = planner().plan(base_input())
    first, second = previous.work_blocks
    repair_input = replace(
        base_input(),
        busy_intervals=(
                CalendarBusyInterval(
                    "external-conflict",
                    "meeting",
                    first.interval,
                    False,
                    None,
                    2,
                ),
                CalendarBusyInterval(
                    "owned-first",
                    "event-first",
                    first.interval,
                    True,
                    first.work_block_id,
                    2,
                ),
                CalendarBusyInterval(
                    "owned-second",
                    "event-second",
                    second.interval,
                    True,
                    second.work_block_id,
                    2,
                ),
        ),
        preserved_blocks=(
                PreservedWorkBlock(
                    second.work_block_id,
                    second.commitment_id,
                    second.interval,
                    second.duration_minutes,
                    2,
                    "event-second",
                ),
        ),
        known_work_block_ids=(first.work_block_id, second.work_block_id),
        affected_work_block_ids=(first.work_block_id,),
        calendar_state_revision=2,
        calendar_snapshot_hash="after",
    )
    repaired = StablePlanRepairer(planner=planner()).repair(repair_input, previous)
    repaired_by_id = {item.work_block_id: item for item in repaired.work_blocks}

    assert repaired.feasible
    assert repaired_by_id[second.work_block_id].interval == second.interval
    assert repaired_by_id[first.work_block_id].interval.start == second.interval.end
    assert repaired_by_id[first.work_block_id].work_block_id == first.work_block_id
    assert not repaired_by_id[first.work_block_id].interval.overlaps(first.interval)
    assert repaired.risk_audit["_repair"]["moved_block_count"] == 1
    assert repaired.risk_audit["_repair"]["unaffected_blocks_preserved"] == "true"
    arc = repaired.risk_audit[first.commitment_id]
    assert arc["risk_before_repair"] == "critical"
    assert arc["risk_after_repair"] == "on_track"


def test_policy_thresholds_v1_requires_approval_for_three_moves() -> None:
    previous = planner().plan(base_input())
    moved = tuple(
        type(block)(
            work_block_id=block.work_block_id,
            commitment_id=block.commitment_id,
            interval=TimeInterval(
                block.interval.start + timedelta(hours=1),
                block.interval.end + timedelta(hours=1),
            ),
            duration_minutes=block.duration_minutes,
            preserved=block.preserved,
            calendar_event_id=block.calendar_event_id,
        )
        for block in previous.work_blocks
    )
    # A third owned block is sufficient to exercise the frozen count boundary.
    moved += (type(moved[0])(
        work_block_id="third",
        commitment_id="commitment-repair",
        interval=TimeInterval(NOW + timedelta(hours=5), NOW + timedelta(hours=6)),
        duration_minutes=60,
        preserved=True,
        calendar_event_id="event-third",
    ),)
    previous_with_third = replace(
        previous,
        work_blocks=previous.work_blocks + (moved[-1],),
    )
    desired = replace(
        previous,
        planner_run_id="repair-three",
        work_blocks=moved[:-1]
        + (
            type(moved[-1])(
                work_block_id="third",
                commitment_id="commitment-repair",
                interval=TimeInterval(NOW + timedelta(hours=6), NOW + timedelta(hours=7)),
                duration_minutes=60,
                preserved=True,
                calendar_event_id="event-third",
            ),
        ),
    )
    decision = RepairPolicyEvaluator().evaluate(
        PlanDiffer().diff(previous_with_third, desired),
        desired,
        preferences(),
    )
    assert decision.disposition == RepairPolicyDisposition.APPROVAL_REQUIRED
    assert decision.changed_block_count == 3
    assert decision.reason_codes == ("more_than_two_blocks_changed",)


def test_policy_thresholds_v1_enforces_shift_focus_and_daily_limits() -> None:
    previous = planner().plan(base_input())
    first, second = previous.work_blocks
    long_shift_plan = replace(
        previous,
        planner_run_id="long-shift",
        work_blocks=(
            replace(
                first,
                interval=TimeInterval(
                    first.interval.start + timedelta(hours=25),
                    first.interval.end + timedelta(hours=25),
                ),
            ),
            second,
        ),
    )
    long_shift = RepairPolicyEvaluator().evaluate(
        PlanDiffer().diff(previous, long_shift_plan),
        long_shift_plan,
        preferences(),
    )
    assert "single_shift_exceeds_24_hours" in long_shift.reason_codes

    narrow_preferences = replace(
        preferences(),
        preferred_focus_windows=((time(9), time(10)),),
    )
    outside_plan = replace(
        previous,
        planner_run_id="outside-focus",
        work_blocks=(
            replace(
                first,
                interval=TimeInterval(
                    first.interval.start + timedelta(hours=2),
                    first.interval.end + timedelta(hours=2),
                ),
            ),
            second,
        ),
    )
    outside = RepairPolicyEvaluator().evaluate(
        PlanDiffer().diff(previous, outside_plan),
        outside_plan,
        narrow_preferences,
    )
    assert "outside_preferred_focus_period" in outside.reason_codes

    five_blocks = tuple(
        replace(
            first,
            work_block_id=f"daily-{index}",
            interval=TimeInterval(
                NOW + timedelta(hours=index),
                NOW + timedelta(hours=index + 1),
            ),
        )
        for index in range(5)
    )
    daily_plan = replace(
        previous,
        planner_run_id="daily-limit",
        work_blocks=five_blocks,
    )
    daily = RepairPolicyEvaluator().evaluate(
        PlanDiffer().diff(previous, daily_plan),
        daily_plan,
        preferences(),
    )
    assert "daily_focus_limit_exceeded" in daily.reason_codes
