from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from commitmentos.application.dto import Page
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.queries.portfolio_view import portfolio_view
from commitmentos.domain.commitments.models import Commitment, LifecycleStatus, RiskLevel
from commitmentos.domain.planning.models import PortfolioPlan


class ListCommitments:
    """Candidate dashboard listing: newest-updated commitments first, with
    provenance-bearing projection fields when a planner run has published."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def execute(
        self,
        user_id: str,
        lifecycle_status: LifecycleStatus | None,
        risk_level: RiskLevel | None,
        before: datetime | None,
        limit: int,
    ) -> Page:
        async def _load(
            repositories: RepositorySet,
        ) -> list[tuple[Commitment, PortfolioPlan | None]]:
            commitments = list(
                await repositories.commitments.list_for_user(
                    user_id, lifecycle_status, before, limit
                )
            )
            result: list[tuple[Commitment, PortfolioPlan | None]] = []
            plans: dict[str, PortfolioPlan | None] = {}
            for commitment in commitments:
                planner_run_id = (
                    commitment.projection.planner_run_id
                    if commitment.projection is not None
                    else ""
                )
                if planner_run_id and planner_run_id not in plans:
                    plans[planner_run_id] = await repositories.planner_runs.get(
                        planner_run_id
                    )
                plan = plans.get(planner_run_id)
                result.append((commitment, plan))
            return result

        records = await self._unit_of_work.read(_load)
        if risk_level is not None:
            records = [
                (commitment, plan)
                for commitment, plan in records
                if commitment.projection is not None
                and commitment.projection.risk_level == risk_level
            ]
        items = [self._summary(commitment, plan) for commitment, plan in records]
        next_cursor = (
            records[-1][0].updated_at.isoformat()
            if len(records) == limit
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _summary(
        commitment: Commitment,
        plan: PortfolioPlan | None = None,
    ) -> Mapping[str, Any]:
        summary: dict[str, Any] = {
            "commitment_id": commitment.commitment_id,
            "title": commitment.title,
            "ownership_type": commitment.ownership_type.value,
            "lifecycle_status": commitment.lifecycle_status.value,
            "beneficiary": commitment.beneficiary.get("display_name", ""),
            "deadline": {
                "value": commitment.deadline.value.isoformat(),
                "timezone": commitment.deadline.timezone,
                "source_expression": commitment.deadline.source_expression,
                "confidence": commitment.deadline.confidence,
            },
            "effort": {
                "proposed_minutes": commitment.effort.proposed_minutes,
                "confirmed_minutes": commitment.effort.confirmed_minutes,
            },
            "revision": commitment.revision,
            "plan_revision": commitment.plan_revision,
            "explicit_priority": commitment.explicit_priority,
            "last_reconciled_at": (
                commitment.last_reconciled_at.isoformat()
                if commitment.last_reconciled_at is not None
                else None
            ),
            "updated_at": commitment.updated_at.isoformat(),
            "projection": None,
            "portfolio": portfolio_view(commitment, plan),
        }
        if commitment.projection is not None:
            summary["projection"] = {
                "risk_level": commitment.projection.risk_level.value,
                "remaining_minutes": commitment.projection.remaining_minutes,
                "verified_completed_minutes": (
                    commitment.projection.verified_completed_minutes
                ),
                "blocking_status": commitment.projection.blocking_status.value,
                "planner_run_id": commitment.projection.planner_run_id,
                "current": (
                    commitment.projection.source_commitment_revision
                    == commitment.revision
                ),
            }
        return summary
