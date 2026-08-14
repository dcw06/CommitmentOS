from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.schemas import CommandResponse, WorkCheckInApiRequest
from commitmentos.application.commands.record_work_check_in import (
    RecordWorkCheckIn,
    WorkCheckInRequest,
)
from commitmentos.application.dto import AuthenticatedActor, CommandStatus


class WorkBlocksRouter:
    def __init__(
        self,
        record_work_check_in: RecordWorkCheckIn,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
    ) -> None:
        self._record_work_check_in = record_work_check_in
        self._session = session
        self._csrf = csrf

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["work-blocks"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.post(
            "/work-blocks/{work_block_id}/check-in",
            response_model=CommandResponse,
        )
        async def check_in(
            work_block_id: str,
            body: WorkCheckInApiRequest,
            request: Request,
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return await handler.check_in(actor, work_block_id, body, trace_id)

        return router

    async def check_in(
        self,
        actor: AuthenticatedActor,
        work_block_id: str,
        request: WorkCheckInApiRequest,
        trace_id: str,
    ) -> CommandResponse:
        try:
            result = await self._record_work_check_in.execute(
                actor,
                WorkCheckInRequest(
                    work_block_id=work_block_id,
                    idempotency_key=request.idempotency_key,
                    completed=request.completed,
                    verified_minutes=request.verified_minutes,
                    checked_in_at=request.checked_in_at,
                    expected_revision=request.expected_revision,
                ),
                trace_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.status == CommandStatus.TERMINAL_FAILURE:
            if result.error_code == "work_block_not_found":
                raise HTTPException(status_code=404, detail=result.error_code)
            if result.error_code == "work_block_forbidden":
                raise HTTPException(status_code=403, detail=result.error_code)
            raise HTTPException(status_code=409, detail=result.error_code or "check_in_failed")
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
