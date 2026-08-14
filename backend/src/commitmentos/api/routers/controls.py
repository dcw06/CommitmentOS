from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.schemas import CommandResponse, ControlChangeApiRequest
from commitmentos.application.commands.change_system_control import ChangeSystemControl
from commitmentos.application.dto import (
    AuthenticatedActor,
    CommandStatus,
    ControlChangeRequest,
)


class ControlsRouter:
    def __init__(
        self,
        change_system_control: ChangeSystemControl,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
    ) -> None:
        self._change_system_control = change_system_control
        self._session = session
        self._csrf = csrf

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["controls"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.post("/controls/change", response_model=CommandResponse)
        async def change(
            body: ControlChangeApiRequest,
            request: Request,
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return await handler.change(actor, body, trace_id)

        return router

    async def change(
        self,
        actor: AuthenticatedActor,
        request: ControlChangeApiRequest,
        trace_id: str,
    ) -> CommandResponse:
        try:
            result = await self._change_system_control.execute(
                actor,
                ControlChangeRequest(
                    control_name=request.control_name,
                    target_mode=request.target_mode,
                    reason=request.reason,
                    expected_control_epoch=request.expected_control_epoch,
                ),
                trace_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.status == CommandStatus.TERMINAL_FAILURE:
            raise HTTPException(status_code=409, detail=result.error_code or "control_change_failed")
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
