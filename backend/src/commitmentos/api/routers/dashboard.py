from __future__ import annotations

import binascii
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.application.queries.get_today import GetToday
from commitmentos.application.queries.list_activity import ListActivity


class DashboardRouter:
    def __init__(
        self,
        get_today: GetToday,
        list_activity: ListActivity,
        get_system_status: GetSystemStatus,
        session: ControlledSessionDependency,
        default_timezone: str,
    ) -> None:
        self._get_today = get_today
        self._list_activity = list_activity
        self._get_system_status = get_system_status
        self._session = session
        self._default_timezone = default_timezone

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
        handler = self
        session = self._session

        @router.get("/today")
        async def today(
            actor: AuthenticatedActor = Depends(session),
            timezone: str | None = Query(default=None),
        ) -> Any:
            return await handler.today(actor, timezone or handler._default_timezone)

        @router.get("/activity")
        async def activity(
            actor: AuthenticatedActor = Depends(session),
            cursor: str | None = Query(default=None),
            limit: int = Query(default=50, ge=1, le=100),
        ) -> Any:
            return await handler.activity(actor, cursor, limit)

        @router.get("/system-status")
        async def system_status(
            actor: AuthenticatedActor = Depends(session),
        ) -> Any:
            return await handler.system_status(actor)

        return router

    async def today(self, actor: AuthenticatedActor, timezone: str) -> Any:
        try:
            view = await self._get_today.execute(actor.user_id, timezone)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail="unknown timezone") from error
        return asdict(view)

    async def activity(
        self,
        actor: AuthenticatedActor,
        cursor: str | None,
        limit: int,
    ) -> Any:
        try:
            before = self._list_activity._decode_cursor(cursor)[0] if cursor else None
        except (ValueError, TypeError, KeyError, binascii.Error) as error:
            raise HTTPException(status_code=400, detail="invalid activity cursor") from error
        page = await self._list_activity.execute(actor.user_id, before, limit)
        return {"items": list(page.items), "next_cursor": page.next_cursor}

    async def system_status(self, actor: AuthenticatedActor) -> Any:
        return asdict(await self._get_system_status.execute(actor.user_id))
