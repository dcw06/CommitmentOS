from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.queries.get_commitment import GetCommitment
from commitmentos.application.queries.list_commitments import ListCommitments
from commitmentos.domain.commitments.models import LifecycleStatus, RiskLevel


class CommitmentsRouter:
    """Candidate dashboard reads: commitment listing and the source evidence
    view. Read-only routes sit behind the server-side session (no CSRF —
    no state changes); mutation routes (completion, check-in) are Phase 5
    scope and are not registered here.
    """

    def __init__(
        self,
        get_commitment: GetCommitment,
        list_commitments: ListCommitments,
        session: ControlledSessionDependency,
    ) -> None:
        self._get_commitment = get_commitment
        self._list_commitments = list_commitments
        self._session = session

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["commitments"])
        session = self._session
        handler = self

        @router.get("/commitments")
        async def list_commitments(
            actor: AuthenticatedActor = Depends(session),
            lifecycle_status: str | None = Query(default=None),
            risk_level: str | None = Query(default=None),
            limit: int = Query(default=25, ge=1, le=100),
        ) -> Any:
            return await handler.list(actor, lifecycle_status, risk_level, None, limit)

        @router.get("/commitments/{commitment_id}")
        async def get_commitment(
            commitment_id: str,
            actor: AuthenticatedActor = Depends(session),
        ) -> Any:
            return await handler.get(actor, commitment_id)

        return router

    async def list(
        self,
        actor: AuthenticatedActor,
        lifecycle_status: str | None,
        risk_level: str | None,
        cursor: str | None,
        limit: int,
    ) -> Any:
        try:
            status_filter = (
                LifecycleStatus(lifecycle_status) if lifecycle_status else None
            )
            risk_filter = RiskLevel(risk_level) if risk_level else None
        except ValueError as error:
            raise HTTPException(status_code=400, detail="unknown filter value") from error
        page = await self._list_commitments.execute(
            actor.user_id, status_filter, risk_filter, None, limit
        )
        return {"items": list(page.items), "next_cursor": page.next_cursor}

    async def get(self, actor: AuthenticatedActor, commitment_id: str) -> Any:
        detail = await self._get_commitment.execute(actor.user_id, commitment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="commitment not found")
        commitment = detail.commitment
        return {
            "commitment": {
                "commitment_id": commitment.commitment_id,
                "title": commitment.title,
                "description": commitment.description,
                "ownership_type": commitment.ownership_type.value,
                "lifecycle_status": commitment.lifecycle_status.value,
                "beneficiary": commitment.beneficiary.get("display_name", ""),
                "semantic_fingerprint": commitment.semantic_fingerprint,
                "source_thread_id": commitment.source_thread_id,
                "deadline": {
                    "value": commitment.deadline.value.isoformat(),
                    "timezone": commitment.deadline.timezone,
                    "source_expression": commitment.deadline.source_expression,
                    "confidence": commitment.deadline.confidence,
                    "rule_version": commitment.deadline.rule_version,
                },
                "effort": {
                    "proposed_minutes": commitment.effort.proposed_minutes,
                    "confirmed_minutes": commitment.effort.confirmed_minutes,
                },
                "revision": commitment.revision,
                "plan_revision": commitment.plan_revision,
                "created_at": commitment.created_at.isoformat(),
                "updated_at": commitment.updated_at.isoformat(),
            },
            # The source evidence view: minimal excerpts plus references, never
            # message bodies (plan §13.3).
            "evidence": [
                {
                    "evidence_id": item.get("evidence_id", ""),
                    "excerpt": item.get("excerpt", ""),
                    "span_key": item.get("span_key", ""),
                    "source_reference": dict(item.get("source_reference", {})),
                    "confidence": item.get("confidence"),
                    "model_version": item.get("model_version", ""),
                    "created_at": (
                        item["created_at"].isoformat()
                        if item.get("created_at") is not None
                        else None
                    ),
                }
                for item in detail.evidence
            ],
            "work_blocks": [
                {
                    "work_block_id": block.work_block_id,
                    "scheduled_start": block.scheduled_start.isoformat(),
                    "scheduled_end": block.scheduled_end.isoformat(),
                    "execution_state": block.execution_state.value,
                    "verified_minutes": block.verified_minutes,
                    "plan_revision": block.plan_revision,
                    # The guarded check-in route requires the block's expected
                    # revision; the dashboard reads it from here.
                    "revision": block.revision,
                }
                for block in detail.work_blocks
            ],
            "activity": [
                {
                    "event_type": event.event_type.value,
                    "summary": event.summary,
                    "actor": event.actor,
                    "created_at": event.created_at.isoformat(),
                }
                for event in detail.activity
            ],
        }
