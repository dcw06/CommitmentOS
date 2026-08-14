from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from commitmentos.domain.commitments.models import RiskLevel
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CandidateSlot,
    CommitmentDemand,
    PlannedWorkBlock,
    PlannerInput,
    PortfolioPlan,
    PreservedWorkBlock,
    TimeInterval,
)
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.planning.scoring import SlotScorer

REPAIR_OBJECTIVE_VERSION = "stable-repair-objective-v1"


class StablePlanRepairer:
    """Pure lexicographic implementation of the plan §12.4 objective.

    Calendar classification has already decided which blocks are immutable,
    explicitly adopted, unaffected, or affected. The repairer consumes those
    facts, never policy: immutable/adopted/unaffected blocks are fixed first;
    affected blocks retain their stable identity and choose the nearest valid
    exact-duration slot; the portfolio planner fills only genuinely missing
    capacity after those preservation decisions.
    """

    def __init__(
        self,
        slot_generator: CandidateSlotGenerator | None = None,
        constraint_evaluator: ConstraintEvaluator | None = None,
        planner: PortfolioPlanner | None = None,
    ) -> None:
        self._slots = slot_generator or CandidateSlotGenerator(15)
        self._constraints = constraint_evaluator or ConstraintEvaluator()
        self._planner = planner or PortfolioPlanner(
            self._slots,
            self._constraints,
            SlotScorer("stable-slot-score-v1"),
            RiskCalculator("portfolio-risk-v1"),
        )

    def repair(self, planner_input: PlannerInput, previous_plan: PortfolioPlan) -> PortfolioPlan:
        previous = {block.work_block_id: block for block in previous_plan.work_blocks}
        demand_by_id = {demand.commitment_id: demand for demand in planner_input.demands}
        factual = {block.work_block_id: block for block in planner_input.preserved_blocks}
        immutable_ids = set(planner_input.immutable_work_block_ids)
        adopted_ids = set(planner_input.adopted_work_block_ids)
        explicit_affected = set(planner_input.affected_work_block_ids)

        fixed: dict[str, PreservedWorkBlock] = dict(factual)
        immutable_conflict = False
        for work_block_id in sorted(immutable_ids):
            if work_block_id in fixed:
                continue
            block = previous.get(work_block_id)
            if block is None:
                continue
            fixed[work_block_id] = self._preserved(block)
            if self._overlaps_external_busy(block.interval, planner_input, work_block_id):
                immutable_conflict = True

        affected_ids = explicit_affected or {
            work_block_id
            for work_block_id in previous
            if work_block_id not in fixed
        }
        # Priority 2 and 3: immutable and explicitly adopted blocks are never
        # candidates for optimization. An adopted block's factual interval is
        # already in `fixed` and therefore wins over its old approved slot.
        movable_ids = sorted(affected_ids - immutable_ids - adopted_ids)
        movable_ids.sort(
            key=lambda work_block_id: (
                previous[work_block_id].interval.start.astimezone(timezone.utc)
                if work_block_id in previous
                else planner_input.planning_horizon.end.astimezone(timezone.utc),
                work_block_id,
            )
        )

        busy = tuple(
            interval
            for interval in planner_input.busy_intervals
            if not (
                interval.is_app_owned
                and interval.work_block_id in set(movable_ids) | adopted_ids
            )
        )
        occupied: list[TimeInterval] = [interval.interval for interval in busy]
        occupied.extend(block.interval for block in fixed.values())
        daily_focus: list[TimeInterval] = [block.interval for block in fixed.values()]
        unplaced: list[str] = []
        assignments = self._minimum_change_assignments(
            movable_ids,
            previous,
            demand_by_id,
            planner_input,
            busy,
            tuple(fixed.values()),
            occupied,
            daily_focus,
        )
        if assignments is None:
            unplaced.extend(movable_ids)
            assignments = {}
        for work_block_id in movable_ids:
            chosen = assignments.get(work_block_id)
            old = previous.get(work_block_id)
            if chosen is None or old is None:
                continue
            relocated = PreservedWorkBlock(
                work_block_id=old.work_block_id,
                commitment_id=old.commitment_id,
                interval=chosen.interval,
                duration_minutes=old.duration_minutes,
                plan_revision=max(
                    (
                        item.plan_revision
                        for item in planner_input.preserved_blocks
                        if item.commitment_id == old.commitment_id
                    ),
                    default=0,
                ),
                calendar_event_id=old.calendar_event_id,
            )
            fixed[work_block_id] = relocated
            occupied.append(chosen.interval)
            daily_focus.append(chosen.interval)

        augmented_input = replace(
            planner_input,
            busy_intervals=busy,
            preserved_blocks=tuple(
                sorted(
                    fixed.values(),
                    key=lambda block: (
                        block.interval.start.astimezone(timezone.utc),
                        block.work_block_id,
                    ),
                )
            ),
        )
        repaired = self._planner.plan(augmented_input)
        affected = tuple(sorted(affected_ids))
        displacement = self.displacement_cost(previous_plan, repaired)
        audit: dict[str, dict[str, int | float | str | None]] = {
            key: dict(value) for key, value in repaired.risk_audit.items()
        }
        repaired_allocations = {
            allocation.commitment_id: allocation for allocation in repaired.allocations
        }
        previous_allocations = {
            allocation.commitment_id: allocation for allocation in previous_plan.allocations
        }
        for demand in planner_input.demands:
            valid_reserved = sum(
                block.duration_minutes
                for block in factual.values()
                if block.commitment_id == demand.commitment_id
            )
            detected_deficit = max(demand.remaining_minutes - valid_reserved, 0)
            after = repaired_allocations.get(demand.commitment_id)
            before = previous_allocations.get(demand.commitment_id)
            row = audit.setdefault(demand.commitment_id, {})
            row.update(
                {
                    "detected_allocation_deficit": detected_deficit,
                    "shortfall_before_repair": detected_deficit,
                    "shortfall_after_repair": (
                        after.shortfall_minutes if after is not None else detected_deficit
                    ),
                    "risk_before_repair": (
                        RiskLevel.CRITICAL.value
                        if detected_deficit > 0
                        else (
                            before.risk_level.value
                            if before is not None
                            else demand.previous_risk_level.value
                        )
                    ),
                    "risk_after_repair": (
                        after.risk_level.value
                        if after is not None
                        else RiskLevel.CRITICAL.value
                    ),
                }
            )
        audit["_repair"] = {
            "objective_version": REPAIR_OBJECTIVE_VERSION,
            "previous_planner_run_id": previous_plan.planner_run_id,
            "affected_work_block_ids": ",".join(affected),
            "immutable_work_block_ids": ",".join(sorted(immutable_ids)),
            "adopted_work_block_ids": ",".join(sorted(adopted_ids)),
            "moved_block_count": sum(
                1
                for work_block_id in affected
                if work_block_id in previous
                and self._block_interval(repaired, work_block_id)
                != previous[work_block_id].interval
            ),
            "total_displacement_minutes": displacement,
            "unplaced_work_block_ids": ",".join(sorted(unplaced)),
            "immutable_conflict": str(immutable_conflict).lower(),
            "unaffected_blocks_preserved": str(
                self._preserves_ids(previous_plan, repaired, set(affected))
            ).lower(),
        }
        return replace(
            repaired,
            feasible=repaired.feasible and not unplaced and not immutable_conflict,
            risk_audit=audit,
        )

    def _minimum_change_assignments(
        self,
        movable_ids: list[str],
        previous: dict[str, PlannedWorkBlock],
        demand_by_id: dict[str, CommitmentDemand],
        planner_input: PlannerInput,
        busy: tuple[CalendarBusyInterval, ...],
        fixed: tuple[PreservedWorkBlock, ...],
        base_occupied: list[TimeInterval],
        base_daily_focus: list[TimeInterval],
    ) -> dict[str, CandidateSlot] | None:
        """Solve priorities 5 and 6 lexicographically over valid assignments.

        A greedy per-block nearest slot can move more blocks than necessary or
        strand a later block. This bounded exact search first requires a full
        hard-constraint-safe assignment, then minimizes the number of moved
        blocks, then total displacement, with timestamps and IDs only as the
        deterministic final tie-break.
        """
        free = self._slots.free_capacity(
            planner_input.planning_horizon,
            busy,
            fixed,
            planner_input.preferences,
        )
        candidates_by_id: dict[str, tuple[CandidateSlot, ...]] = {}
        exact_demands: dict[str, CommitmentDemand] = {}
        for work_block_id in movable_ids:
            old = previous.get(work_block_id)
            raw_demand = demand_by_id.get(old.commitment_id) if old is not None else None
            if old is None or not isinstance(raw_demand, CommitmentDemand):
                return None
            exact_demand = replace(raw_demand, remaining_minutes=old.duration_minutes)
            exact_demands[work_block_id] = exact_demand
            candidates = tuple(
                sorted(
                    (
                        candidate
                        for candidate in self._slots.generate(
                            free,
                            exact_demand,
                            planner_input.preferences,
                            planner_input.planning_horizon.start,
                        )
                        if candidate.interval.duration_minutes() == old.duration_minutes
                        and self._constraints.is_valid(
                            candidate.interval,
                            exact_demand,
                            planner_input.preferences,
                            base_occupied,
                            planner_input.planning_horizon.start,
                            base_daily_focus,
                        )
                    ),
                    key=lambda candidate: (
                        int(candidate.interval != old.interval),
                        self._interval_displacement(old.interval, candidate.interval),
                        candidate.interval.start.astimezone(timezone.utc),
                        candidate.interval.end.astimezone(timezone.utc),
                    ),
                )
            )
            if not candidates:
                return None
            candidates_by_id[work_block_id] = candidates

        search_order = sorted(
            movable_ids,
            key=lambda work_block_id: (
                len(candidates_by_id[work_block_id]),
                previous[work_block_id].interval.start.astimezone(timezone.utc),
                work_block_id,
            ),
        )
        minimum_move = {
            work_block_id: min(
                int(candidate.interval != previous[work_block_id].interval)
                for candidate in candidates_by_id[work_block_id]
            )
            for work_block_id in search_order
        }
        minimum_displacement = {
            work_block_id: min(
                self._interval_displacement(
                    previous[work_block_id].interval,
                    candidate.interval,
                )
                for candidate in candidates_by_id[work_block_id]
            )
            for work_block_id in search_order
        }
        best_objective: tuple[int, int, tuple[tuple[str, datetime], ...]] | None = None
        best: dict[str, CandidateSlot] | None = None
        selected: dict[str, CandidateSlot] = {}

        def _search(
            index: int,
            occupied: list[TimeInterval],
            daily_focus: list[TimeInterval],
            moved: int,
            displacement: int,
        ) -> None:
            nonlocal best, best_objective
            remaining = search_order[index:]
            lower_moved = moved + sum(minimum_move[item] for item in remaining)
            lower_displacement = displacement + sum(
                minimum_displacement[item] for item in remaining
            )
            if best_objective is not None and (
                lower_moved > best_objective[0]
                or (
                    lower_moved == best_objective[0]
                    and lower_displacement > best_objective[1]
                )
            ):
                return
            if index == len(search_order):
                signature = tuple(
                    (
                        work_block_id,
                        selected[work_block_id].interval.start.astimezone(timezone.utc),
                    )
                    for work_block_id in sorted(selected)
                )
                objective = (moved, displacement, signature)
                if best_objective is None or objective < best_objective:
                    best_objective = objective
                    best = dict(selected)
                return
            work_block_id = search_order[index]
            old = previous[work_block_id]
            exact_demand = exact_demands[work_block_id]
            for candidate in candidates_by_id[work_block_id]:
                if not self._constraints.is_valid(
                    candidate.interval,
                    exact_demand,
                    planner_input.preferences,
                    occupied,
                    planner_input.planning_horizon.start,
                    daily_focus,
                ):
                    continue
                selected[work_block_id] = candidate
                changed = int(candidate.interval != old.interval)
                cost = self._interval_displacement(old.interval, candidate.interval)
                _search(
                    index + 1,
                    occupied + [candidate.interval],
                    daily_focus + [candidate.interval],
                    moved + changed,
                    displacement + cost,
                )
                del selected[work_block_id]

        _search(0, list(base_occupied), list(base_daily_focus), 0, 0)
        return best

    def displacement_cost(self, previous_plan: PortfolioPlan, repaired_plan: PortfolioPlan) -> int:
        previous = {block.work_block_id: block for block in previous_plan.work_blocks}
        repaired = {block.work_block_id: block for block in repaired_plan.work_blocks}
        return sum(
            self._interval_displacement(block.interval, repaired[work_block_id].interval)
            for work_block_id, block in previous.items()
            if work_block_id in repaired and block.interval != repaired[work_block_id].interval
        )

    def preserves_unaffected_blocks(
        self,
        previous_plan: PortfolioPlan,
        repaired_plan: PortfolioPlan,
    ) -> bool:
        repair_audit = repaired_plan.risk_audit.get("_repair", {})
        raw_affected = str(repair_audit.get("affected_work_block_ids") or "")
        affected = {value for value in raw_affected.split(",") if value}
        return self._preserves_ids(previous_plan, repaired_plan, affected)

    @staticmethod
    def _preserved(block: PlannedWorkBlock) -> PreservedWorkBlock:
        return PreservedWorkBlock(
            work_block_id=block.work_block_id,
            commitment_id=block.commitment_id,
            interval=block.interval,
            duration_minutes=block.duration_minutes,
            plan_revision=0,
            calendar_event_id=block.calendar_event_id,
        )

    @staticmethod
    def _interval_displacement(before: TimeInterval, after: TimeInterval) -> int:
        return abs(
            int(
                (
                    after.start.astimezone(timezone.utc)
                    - before.start.astimezone(timezone.utc)
                ).total_seconds()
                // 60
            )
        )

    @staticmethod
    def _block_interval(plan: PortfolioPlan, work_block_id: str) -> TimeInterval | None:
        return next(
            (
                block.interval
                for block in plan.work_blocks
                if block.work_block_id == work_block_id
            ),
            None,
        )

    @staticmethod
    def _preserves_ids(
        previous_plan: PortfolioPlan,
        repaired_plan: PortfolioPlan,
        affected: set[str],
    ) -> bool:
        repaired = {block.work_block_id: block for block in repaired_plan.work_blocks}
        for before in previous_plan.work_blocks:
            if before.work_block_id in affected:
                continue
            after = repaired.get(before.work_block_id)
            if after is None or (
                before.commitment_id != after.commitment_id
                or before.interval != after.interval
                or before.duration_minutes != after.duration_minutes
                or (
                    before.calendar_event_id is not None
                    and before.calendar_event_id != after.calendar_event_id
                )
            ):
                return False
        return True

    @staticmethod
    def _overlaps_external_busy(
        interval: TimeInterval,
        planner_input: PlannerInput,
        work_block_id: str,
    ) -> bool:
        return any(
            busy.interval.overlaps(interval)
            for busy in planner_input.busy_intervals
            if busy.work_block_id != work_block_id
        )
