from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from commitmentos.api.dependencies.google_oidc import (
    GoogleDeliveryPrincipal,
    GoogleOidcDependency,
)
from commitmentos.api.schemas import CommandResponse
from commitmentos.application.commands.run_maintenance import MaintenanceKind, RunMaintenance


class SchedulerRouter:
    def __init__(
        self,
        run_maintenance: RunMaintenance,
        scheduler_oidc: GoogleOidcDependency,
    ) -> None:
        self._run_maintenance = run_maintenance
        self._scheduler_oidc = scheduler_oidc

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/internal/scheduler", tags=["scheduler"])
        oidc = self._scheduler_oidc
        handler = self

        @router.post("/maintenance/{kind}", response_model=CommandResponse)
        async def run(
            kind: str,
            request: Request,
            principal: GoogleDeliveryPrincipal = Depends(oidc),
        ) -> Any:
            trace_id = getattr(request.state, "trace_id", "trace-scheduler")
            return await handler.run(principal, kind, trace_id)

        return router

    async def run(
        self,
        principal: GoogleDeliveryPrincipal,
        kind: str,
        trace_id: str,
    ) -> CommandResponse:
        try:
            maintenance_kind = MaintenanceKind(kind)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="unknown maintenance kind") from error
        result = await self._run_maintenance.execute(maintenance_kind, trace_id)
        return CommandResponse(
            status=result.status.value,
            identifiers=dict(result.identifiers),
            revision=result.revision,
            error_code=result.error_code,
        )
