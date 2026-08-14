from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from commitmentos.application.dto import Page
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.domain.commitments.models import Commitment, LifecycleStatus, RiskLevel


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
        async def _load(repositories: RepositorySet) -> list[Commitment]:
            return list(
                await repositories.commitments.list_for_user(
                    user_id, lifecycle_status, before, limit
                )
            )

        commitments = await self._unit_of_work.read(_load)
        if risk_level is not None:
            commitments = [
                commitment
                for commitment in commitments
                if commitment.projection is not None
                and commitment.projection.risk_level == risk_level
            ]
        items = [self._summary(commitment) for commitment in commitments]
        next_cursor = (
            commitments[-1].updated_at.isoformat()
            if len(commitments) == limit
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _summary(commitment: Commitment) -> Mapping[str, Any]:
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
            "updated_at": commitment.updated_at.isoformat(),
            "projection": None,
        }
        if commitment.projection is not None:
            summary["projection"] = {
                "risk_level": commitment.projection.risk_level.value,
                "remaining_minutes": commitment.projection.remaining_minutes,
                "verified_completed_minutes": (
                    commitment.projection.verified_completed_minutes
                ),
                "planner_run_id": commitment.projection.planner_run_id,
            }
        return summary
