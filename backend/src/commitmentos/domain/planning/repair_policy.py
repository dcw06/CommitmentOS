from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from commitmentos.domain.planning.models import (
    PlanDiff,
    PlanMutationType,
    PortfolioPlan,
    UserPlanningPreferences,
)

POLICY_THRESHOLDS_VERSION = "policy_thresholds_v1"
MAX_AUTOMATIC_CHANGED_BLOCKS = 2
MAX_AUTOMATIC_SHIFT_MINUTES = 24 * 60


class RepairPolicyDisposition(StrEnum):
    AUTOMATIC = "automatic"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class RepairPolicyDecision:
    disposition: RepairPolicyDisposition
    reason_codes: tuple[str, ...]
    changed_block_count: int
    maximum_shift_minutes: int
    threshold_version: str = POLICY_THRESHOLDS_VERSION


class RepairPolicyEvaluator:
    """Frozen Phase 4 policy boundary; the planner itself has no policy."""

    def evaluate(
        self,
        plan_diff: PlanDiff,
        repaired_plan: PortfolioPlan,
        preferences: UserPlanningPreferences,
    ) -> RepairPolicyDecision:
        changed = tuple(
            mutation
            for mutation in plan_diff.mutations
            if mutation.mutation_type in (PlanMutationType.MOVE, PlanMutationType.CANCEL)
        )
        shifts = tuple(
            abs(
                int(
                    (
                        mutation.after.start.astimezone(timezone.utc)
                        - mutation.before.start.astimezone(timezone.utc)
                    ).total_seconds()
                    // 60
                )
            )
            for mutation in changed
            if mutation.mutation_type == PlanMutationType.MOVE
            and mutation.before is not None
            and mutation.after is not None
        )
        reasons: list[str] = []
        if len(changed) > MAX_AUTOMATIC_CHANGED_BLOCKS:
            reasons.append("more_than_two_blocks_changed")
        if shifts and max(shifts) > MAX_AUTOMATIC_SHIFT_MINUTES:
            reasons.append("single_shift_exceeds_24_hours")
        if any(
            mutation.after is not None
            and not self._within_preferred_focus(mutation.after, preferences)
            for mutation in changed
            if mutation.mutation_type == PlanMutationType.MOVE
        ):
            reasons.append("outside_preferred_focus_period")
        if self._exceeds_daily_focus(repaired_plan, preferences):
            reasons.append("daily_focus_limit_exceeded")
        return RepairPolicyDecision(
            disposition=(
                RepairPolicyDisposition.APPROVAL_REQUIRED
                if reasons
                else RepairPolicyDisposition.AUTOMATIC
            ),
            reason_codes=tuple(reasons),
            changed_block_count=len(changed),
            maximum_shift_minutes=max(shifts, default=0),
        )

    @staticmethod
    def _within_preferred_focus(interval, preferences: UserPlanningPreferences) -> bool:
        if not preferences.preferred_focus_windows:
            return True
        zone = ZoneInfo(preferences.timezone)
        local_start = interval.start.astimezone(zone)
        local_end = interval.end.astimezone(zone)
        if local_start.date() != local_end.date():
            return False
        return any(
            window_start <= local_start.time() and local_end.time() <= window_end
            for window_start, window_end in preferences.preferred_focus_windows
        )

    @staticmethod
    def _exceeds_daily_focus(
        plan: PortfolioPlan,
        preferences: UserPlanningPreferences,
    ) -> bool:
        zone = ZoneInfo(preferences.timezone)
        by_day: dict[object, int] = {}
        for block in plan.work_blocks:
            local_day = block.interval.start.astimezone(zone).date()
            by_day[local_day] = by_day.get(local_day, 0) + block.duration_minutes
        return any(
            minutes > preferences.daily_focus_limit_minutes
            for minutes in by_day.values()
        )
