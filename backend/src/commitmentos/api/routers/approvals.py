from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.schemas import ApprovalResolutionRequest, CommandResponse
from commitmentos.application.commands.resolve_approval import ResolveApproval
from commitmentos.application.dto import AuthenticatedActor, CommandStatus


class ApprovalsRouter:
    def __init__(
        self,
        resolve_approval: ResolveApproval,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
    ) -> None:
        self._resolve_approval = resolve_approval
        self._session = session
        self._csrf = csrf

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["approvals"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.post("/approvals/{approval_id}/resolve", response_model=CommandResponse)
        async def resolve(
            approval_id: str,
            body: ApprovalResolutionRequest,
            request: Request,
            # Dependency order matters: the session dependency stashes the
            # CSRF secret that the CSRF dependency compares against, and both
            # resolve before request-body validation (§16.3).
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return await handler.resolve(actor, approval_id, body, trace_id)

        return router

    async def resolve(
        self,
        actor: AuthenticatedActor,
        approval_id: str,
        request: ApprovalResolutionRequest,
        trace_id: str,
    ) -> CommandResponse:
        decision: dict[str, Any] = {"decision": request.decision}
        if request.reason is not None:
            decision["reason"] = request.reason
        if request.confirmed_minutes is not None:
            decision["confirmed_minutes"] = request.confirmed_minutes
        if request.deadline is not None:
            decision["deadline"] = request.deadline.isoformat()
        if request.ownership_type is not None:
            decision["ownership_type"] = request.ownership_type
        if request.choice is not None:
            decision["choice"] = request.choice
        try:
            result = await self._resolve_approval.execute(
                actor,
                approval_id,
                decision,
                request.expected_revision,
                trace_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.status == CommandStatus.TERMINAL_FAILURE:
            raise HTTPException(status_code=409, detail=result.error_code or "approval_failed")
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
