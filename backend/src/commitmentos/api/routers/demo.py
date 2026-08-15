from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from commitmentos.application.ports.demo_read_model import DemoReadModel


class DemoRouter:
    """Read-only seeded judge mode (plan §7.4 route class 4, §13.5).

    Serves only repository-owned seeded data through a client with no live
    Firestore, credential, or Google API access. Every mutation method under
    `/demo` is rejected before any handler logic, so a rejected attempt has
    zero durable or external side effects by construction.
    """

    def __init__(self, read_model: DemoReadModel) -> None:
        self._read_model = read_model

    def build(self) -> APIRouter:
        router = APIRouter(tags=["demo"])
        handler = self

        # Data endpoints live under /demo/api so they can never shadow the
        # SPA page URLs (/demo/commitments, /demo/activity) that the dashboard
        # catch-all serves for hard refreshes and shared deep links.
        @router.get("/demo/api/today")
        async def today() -> Response:
            return await handler.today()

        @router.get("/demo/api/commitments")
        async def commitments() -> Response:
            return await handler.commitments()

        @router.get("/demo/api/activity")
        async def activity() -> Response:
            return await handler.activity()

        @router.api_route(
            "/demo/{rest:path}",
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def demo_mutation(rest: str) -> Response:
            raise HTTPException(status_code=403, detail="demo mode is read-only")

        return router

    async def today(self) -> Response:
        return JSONResponse(dict(await self._read_model.get_today()))

    async def commitments(self) -> Response:
        return JSONResponse(list(await self._read_model.list_commitments()))

    async def activity(self) -> Response:
        return JSONResponse(list(await self._read_model.list_activity()))
