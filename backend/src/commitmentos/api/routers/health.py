from __future__ import annotations

from fastapi import APIRouter

from commitmentos.application.ports.unit_of_work import UnitOfWork


class HealthRouter:
    def __init__(self, unit_of_work: UnitOfWork | None, application_version: str) -> None:
        self._unit_of_work = unit_of_work
        self._application_version = application_version

    def build(self) -> APIRouter:
        router = APIRouter(prefix="/health", tags=["health"])
        router.add_api_route("/live", self.live, methods=["GET"])
        return router

    async def live(self) -> dict[str, str]:
        return {"status": "live", "version": self._application_version}
