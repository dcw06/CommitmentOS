from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.schemas import CommandResponse, PlanUndoApiRequest
from commitmentos.application.commands.request_plan_undo import (
    PlanUndoRequest,
    RequestPlanUndo,
)
from commitmentos.application.dto import AuthenticatedActor, CommandStatus


class PlansRouter:
    def __init__(
        self,
        request_plan_undo: RequestPlanUndo,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
    ) -> None:
        self._request_plan_undo = request_plan_undo
        self._session = session
        self._csrf = csrf

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["plans"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.post(
            "/plans/{planner_run_id}/undo",
            response_model=CommandResponse,
        )
        async def request_undo(
            planner_run_id: str,
            body: PlanUndoApiRequest,
            request: Request,
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return await handler.request_undo(actor, planner_run_id, body, trace_id)

        return router

    async def request_undo(
        self,
        actor: AuthenticatedActor,
        planner_run_id: str,
        request: PlanUndoApiRequest,
        trace_id: str,
    ) -> CommandResponse:
        try:
            result = await self._request_plan_undo.execute(
                actor,
                PlanUndoRequest(planner_run_id, request.idempotency_key),
                trace_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.status == CommandStatus.TERMINAL_FAILURE:
            if result.error_code == "planner_run_not_found":
                raise HTTPException(status_code=404, detail=result.error_code)
            if result.error_code == "planner_run_forbidden":
                raise HTTPException(status_code=403, detail=result.error_code)
            raise HTTPException(status_code=409, detail=result.error_code or "undo_failed")
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
