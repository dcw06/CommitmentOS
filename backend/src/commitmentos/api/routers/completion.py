from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.schemas import CommandResponse, CompleteCommitmentApiRequest
from commitmentos.application.commands.complete_commitment import (
    CompleteCommitment,
    CompleteCommitmentRequest,
)
from commitmentos.application.dto import AuthenticatedActor, CommandStatus


class CompletionRouter:
    def __init__(
        self,
        complete_commitment: CompleteCommitment,
        session: ControlledSessionDependency,
        csrf: CsrfProtection,
    ) -> None:
        self._complete_commitment = complete_commitment
        self._session = session
        self._csrf = csrf

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1", tags=["commitments"])
        session = self._session
        csrf = self._csrf
        handler = self

        @router.post(
            "/commitments/{commitment_id}/complete",
            response_model=CommandResponse,
        )
        async def complete(
            commitment_id: str,
            body: CompleteCommitmentApiRequest,
            request: Request,
            actor: AuthenticatedActor = Depends(session),
            _csrf: None = Depends(csrf),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-unset")
            return await handler.complete(actor, commitment_id, body, trace_id)

        return router

    async def complete(
        self,
        actor: AuthenticatedActor,
        commitment_id: str,
        request: CompleteCommitmentApiRequest,
        trace_id: str,
    ) -> CommandResponse:
        try:
            result = await self._complete_commitment.execute(
                actor,
                CompleteCommitmentRequest(
                    commitment_id=commitment_id,
                    idempotency_key=request.idempotency_key,
                    completed_at=request.completed_at,
                    expected_revision=request.expected_revision,
                    note=request.note,
                ),
                trace_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.status == CommandStatus.TERMINAL_FAILURE:
            if result.error_code == "commitment_not_found":
                raise HTTPException(status_code=404, detail=result.error_code)
            if result.error_code == "commitment_forbidden":
                raise HTTPException(status_code=403, detail=result.error_code)
            raise HTTPException(status_code=409, detail=result.error_code or "completion_failed")
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
