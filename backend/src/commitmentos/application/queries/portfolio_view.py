from __future__ import annotations

from typing import Any, Mapping

from commitmentos.domain.commitments.models import Commitment
from commitmentos.domain.planning.models import PortfolioPlan


def portfolio_view(
    commitment: Commitment,
    plan: PortfolioPlan | None,
) -> Mapping[str, Any] | None:
    """Return planner-owned allocation fields only when their provenance is current."""

    if plan is None or commitment.projection is None:
        return None
    if commitment.projection.planner_run_id != plan.planner_run_id:
        return None
    if commitment.projection.source_commitment_revision != commitment.revision:
        return None
    allocation = next(
        (
            item
            for item in plan.allocations
            if item.commitment_id == commitment.commitment_id
        ),
        None,
    )
    if allocation is None or commitment.commitment_id not in plan.commitment_order:
        return None
    return {
        "planner_run_id": plan.planner_run_id,
        "portfolio_position": plan.commitment_order.index(commitment.commitment_id) + 1,
        "portfolio_size": len(plan.commitment_order),
        "allocated_work_minutes": allocation.allocated_work_minutes,
        "preserved_reserved_minutes": allocation.preserved_reserved_minutes,
        "newly_allocated_minutes": allocation.newly_allocated_minutes,
        "shortfall_minutes": allocation.shortfall_minutes,
        "shared_buffer_minutes": allocation.portfolio_slack_minutes,
        "projected_finish": (
            allocation.projected_finish.isoformat()
            if allocation.projected_finish is not None
            else None
        ),
        "risk_level": allocation.risk_level.value,
    }
