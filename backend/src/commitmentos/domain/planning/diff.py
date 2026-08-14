from __future__ import annotations

from datetime import timezone

from commitmentos.domain.planning.models import (
    PlanDiff,
    PlanMutation,
    PlanMutationType,
    PortfolioPlan,
)


class PlanDiffer:
    def diff(self, previous_plan: PortfolioPlan | None, desired_plan: PortfolioPlan) -> PlanDiff:
        previous = {
            item.work_block_id: item for item in previous_plan.work_blocks
        } if previous_plan is not None else {}
        desired = {item.work_block_id: item for item in desired_plan.work_blocks}
        mutations: list[PlanMutation] = []
        preserved: list[str] = []
        displacement = 0
        for work_block_id in sorted(set(previous) | set(desired)):
            before = previous.get(work_block_id)
            after = desired.get(work_block_id)
            if before is None and after is not None:
                mutations.append(
                    PlanMutation(
                        mutation_type=PlanMutationType.CREATE,
                        work_block_id=work_block_id,
                        commitment_id=after.commitment_id,
                        before=None,
                        after=after.interval,
                        reason="new_capacity_allocation",
                        calendar_event_id=after.calendar_event_id,
                    )
                )
            elif before is not None and after is None:
                mutations.append(
                    PlanMutation(
                        mutation_type=PlanMutationType.CANCEL,
                        work_block_id=work_block_id,
                        commitment_id=before.commitment_id,
                        before=before.interval,
                        after=None,
                        reason="allocation_no_longer_required",
                        calendar_event_id=before.calendar_event_id,
                    )
                )
            elif before is not None and after is not None:
                if before.interval == after.interval:
                    preserved.append(work_block_id)
                    continue
                displacement += abs(
                    int(
                        (
                            after.interval.start.astimezone(timezone.utc)
                            - before.interval.start.astimezone(timezone.utc)
                        ).total_seconds()
                        // 60
                    )
                )
                mutations.append(
                    PlanMutation(
                        mutation_type=PlanMutationType.MOVE,
                        work_block_id=work_block_id,
                        commitment_id=after.commitment_id,
                        before=before.interval,
                        after=after.interval,
                        reason="constraint_safe_reallocation",
                        # Persisted identity wins. A later desired revision
                        # cannot substitute a newly derived Calendar ID.
                        calendar_event_id=before.calendar_event_id,
                    )
                )
        return PlanDiff(
            mutations=tuple(mutations),
            preserved_work_block_ids=tuple(preserved),
            moved_block_count=sum(
                item.mutation_type == PlanMutationType.MOVE for item in mutations
            ),
            total_displacement_minutes=displacement,
            metadata={
                "previous_planner_run_id": (
                    previous_plan.planner_run_id if previous_plan is not None else ""
                ),
                "desired_planner_run_id": desired_plan.planner_run_id,
            },
        )

    def validate_owned_targets(self, plan_diff: PlanDiff, owned_work_block_ids: set[str]) -> bool:
        return all(
            mutation.mutation_type == PlanMutationType.CREATE
            or mutation.work_block_id in owned_work_block_ids
            for mutation in plan_diff.mutations
        )
