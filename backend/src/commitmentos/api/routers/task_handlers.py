from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from commitmentos.api.dependencies.google_oidc import (
    GoogleDeliveryPrincipal,
    GoogleOidcDependency,
)
from commitmentos.api.schemas import CommandResponse
from commitmentos.application.commands.execute_calendar_action import ExecuteCalendarAction
from commitmentos.application.commands.reconcile_observation import ReconcileObservation
from commitmentos.application.commands.synchronize_source import SynchronizeSource
from commitmentos.application.dto import CommandStatus
from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    ReconcileObservationTaskV1,
    SourceSyncTaskV1,
    SourceType,
)


class ReconcileTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    observation_id: str
    workflow_version: str
    dispatch_generation: int
    trace_id: str


class CalendarActionTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    outbox_id: str
    action_idempotency_key: str
    trace_id: str


class SourceSyncTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    sync_request_id: str
    sync_generation_id: str
    page_sequence: int
    source: SourceType
    user_id: str
    trace_id: str


def _to_response(result: Any) -> CommandResponse:
    if result.status == CommandStatus.RETRYABLE_FAILURE:
        # A retryable task response: Cloud Tasks retries the named task.
        raise HTTPException(status_code=503, detail=result.error_code or "retryable_failure")
    return CommandResponse(
        status=result.status.value,
        identifiers=dict(result.identifiers),
        revision=result.revision,
        error_code=result.error_code,
    )


class TaskHandlersRouter:
    """Cloud Tasks handlers behind the tasks OIDC trust contract.

    OIDC verification is a FastAPI dependency so it resolves before request
    body validation: an unauthenticated caller receives 401/403 without any
    schema feedback (§16.3 — reject before any processing).

    `/internal/tasks/source-sync` runs the shared bounded Gmail/Calendar
    generation protocol. Legacy spike-shaped Calendar bodies are rejected by
    the typed contract.
    """

    def __init__(
        self,
        reconcile_observation: ReconcileObservation,
        execute_calendar_action: ExecuteCalendarAction,
        synchronize_source: SynchronizeSource,
        tasks_oidc: GoogleOidcDependency,
    ) -> None:
        self._reconcile_observation = reconcile_observation
        self._execute_calendar_action = execute_calendar_action
        self._synchronize_source = synchronize_source
        self._tasks_oidc = tasks_oidc

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/internal/tasks", tags=["tasks"])
        oidc = self._tasks_oidc
        handler = self

        @router.post("/reconcile-observation", response_model=CommandResponse)
        async def reconcile(
            body: ReconcileTaskBody,
            principal: GoogleDeliveryPrincipal = Depends(oidc),
        ) -> Any:
            return await handler.reconcile(principal, body)

        @router.post("/execute-calendar-action", response_model=CommandResponse)
        async def calendar_action(
            body: CalendarActionTaskBody,
            principal: GoogleDeliveryPrincipal = Depends(oidc),
        ) -> Any:
            return await handler.calendar_action(principal, body)

        @router.post("/source-sync")
        async def source_sync(
            body: SourceSyncTaskBody,
            principal: GoogleDeliveryPrincipal = Depends(oidc),
        ) -> Any:
            return await handler.source_sync(principal, body)

        return router

    async def source_sync(
        self,
        principal: GoogleDeliveryPrincipal,
        body: SourceSyncTaskBody,
    ) -> Any:
        result = await self._synchronize_source.execute(
            SourceSyncTaskV1(
                schema_version=body.schema_version,
                sync_request_id=body.sync_request_id,
                sync_generation_id=body.sync_generation_id,
                page_sequence=body.page_sequence,
                source=SourceType(body.source),
                user_id=body.user_id,
                trace_id=body.trace_id,
            )
        )
        return _to_response(result)

    async def reconcile(
        self,
        principal: GoogleDeliveryPrincipal,
        body: ReconcileTaskBody,
    ) -> CommandResponse:
        result = await self._reconcile_observation.execute(
            ReconcileObservationTaskV1(
                schema_version=body.schema_version,
                observation_id=body.observation_id,
                workflow_version=body.workflow_version,
                dispatch_generation=body.dispatch_generation,
                trace_id=body.trace_id,
            )
        )
        return _to_response(result)

    async def calendar_action(
        self,
        principal: GoogleDeliveryPrincipal,
        body: CalendarActionTaskBody,
    ) -> CommandResponse:
        result = await self._execute_calendar_action.execute(
            ExecuteCalendarActionTaskV1(
                schema_version=body.schema_version,
                outbox_id=body.outbox_id,
                action_idempotency_key=body.action_idempotency_key,
                trace_id=body.trace_id,
            )
        )
        return _to_response(result)
