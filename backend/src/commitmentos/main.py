from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from commitmentos.api.middleware.error_mapping import ErrorMappingMiddleware
from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.middleware.security_headers import SecurityHeadersMiddleware
from commitmentos.api.routers.approvals import ApprovalsRouter
from commitmentos.api.routers.commitments import CommitmentsRouter
from commitmentos.api.routers.completion import CompletionRouter
from commitmentos.api.routers.controls import ControlsRouter
from commitmentos.api.routers.health import HealthRouter
from commitmentos.api.routers.plans import PlansRouter
from commitmentos.api.routers.pubsub import PubSubRouter
from commitmentos.api.routers.scheduler import SchedulerRouter
from commitmentos.api.routers.task_handlers import TaskHandlersRouter
from commitmentos.api.routers.work_blocks import WorkBlocksRouter
from commitmentos.bootstrap.container import ApplicationContainer
from commitmentos.bootstrap.logging import LoggingConfigurator
from commitmentos.bootstrap.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else Settings.load()
    LoggingConfigurator("commitmentos", resolved.environment.value).configure()
    app = FastAPI(
        title="CommitmentOS",
        version=resolved.application_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ErrorMappingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    container = ApplicationContainer.build(resolved)
    identity = container.identity_dependencies()
    controlled = container.controlled_commands()
    reconciliation = container.reconciliation()
    execution = container.execution()
    maintenance = container.maintenance()
    synchronization = container.synchronization()

    app.include_router(
        HealthRouter(unit_of_work=None, application_version=resolved.application_version).build()
    )
    app.include_router(
        ApprovalsRouter(controlled.resolve_approval, identity.session, identity.csrf).build()
    )
    app.include_router(
        ControlsRouter(controlled.change_system_control, identity.session, identity.csrf).build()
    )
    app.include_router(
        WorkBlocksRouter(
            controlled.record_work_check_in,
            identity.session,
            identity.csrf,
        ).build()
    )
    app.include_router(
        PlansRouter(
            controlled.request_plan_undo,
            identity.session,
            identity.csrf,
        ).build()
    )
    app.include_router(
        CompletionRouter(
            controlled.complete_commitment,
            identity.session,
            identity.csrf,
        ).build()
    )
    queries = container.queries()
    app.include_router(
        CommitmentsRouter(
            queries.get_commitment, queries.list_commitments, identity.session
        ).build()
    )
    app.include_router(container.dashboard_router(queries, identity.session).build())
    app.include_router(
        TaskHandlersRouter(
            reconciliation.reconcile_observation,
            execution.execute_calendar_action,
            synchronization.synchronize_source,
            identity.tasks_oidc,
        ).build()
    )
    app.include_router(
        SchedulerRouter(maintenance.run_maintenance, identity.scheduler_oidc).build()
    )
    app.include_router(
        PubSubRouter(synchronization.receive_gmail_signal, identity.pubsub_oidc).build()
    )
    app.include_router(
        container.calendar_webhook_router(
            synchronization.receive_calendar_signal
        ).build()
    )

    # Phase 5A: the production AuthRouter is the session issuer and the
    # DemoRouter serves seeded judge mode; no spike module remains on any
    # live path (D4). Spike code stays under `spike/` for the Phase 0
    # evidence record and the local watch-management scripts only.
    app.include_router(container.auth_router(identity).build())
    app.include_router(container.demo_router().build())
    _mount_dashboard(app)
    _assert_no_duplicate_routes(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the compiled React dashboard when a build exists.

    One bundle renders in two modes: /app (live data behind the session at
    the API layer) and /demo (the seeded read-only judge mode — its data
    routes are namespaced under /demo/api and registered by DemoRouter before
    this catch-all, so page URLs like /demo/commitments fall through to the
    SPA on hard refresh). Assets resolve absolutely under /app/assets in both
    modes.
    When no build exists (test runs, backend-only development) nothing is
    registered and the API surface is unchanged.
    """
    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        return
    app.mount("/app/assets", StaticFiles(directory=dist / "assets"), name="app-assets")

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse("/app")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{rest:path}", include_in_schema=False)
    async def app_spa(rest: str = "") -> FileResponse:
        del rest
        return FileResponse(index)

    @app.get("/demo", include_in_schema=False)
    @app.get("/demo/{rest:path}", include_in_schema=False)
    async def demo_spa(rest: str = "") -> FileResponse:
        del rest
        return FileResponse(index)


def _assert_no_duplicate_routes(app: FastAPI) -> None:
    """Boot check: no two handlers may claim the same method+path.

    Guards against the Phase 1 incident where a stub silently shadowed the
    spike's working source-sync handler mounted at the same path.
    """
    seen: dict[tuple[str, str], int] = {}
    for wrapper in app.routes:
        router = getattr(wrapper, "original_router", None)
        routes = router.routes if router is not None else [wrapper]
        for route in routes:
            for method in getattr(route, "methods", None) or []:
                key = (method, getattr(route, "path", ""))
                seen[key] = seen.get(key, 0) + 1
    duplicates = [key for key, count in seen.items() if count > 1]
    if duplicates:
        raise RuntimeError(f"duplicate route registrations: {duplicates}")
