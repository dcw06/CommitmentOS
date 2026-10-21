"""Credential-free, read-only portfolio preview. Never use this as the live service."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from commitmentos.api.routers.demo import DemoRouter
from commitmentos.infrastructure.static_demo import StaticDemoReadModel


def create_app() -> FastAPI:
    app = FastAPI(title="CommitmentOS read-only local demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(DemoRouter(StaticDemoReadModel(Path(__file__).parent / 'demo_data')).build())
    dist = Path(__file__).resolve().parents[3] / 'frontend' / 'dist'
    if (dist / 'index.html').is_file():
        app.mount('/app/assets', StaticFiles(directory=dist / 'assets'), name='demo-assets')

        @app.get('/demo')
        @app.get('/demo/{rest:path}')
        async def page(rest: str = '') -> FileResponse:
            return FileResponse(dist / 'index.html')

    @app.get('/')
    async def root() -> RedirectResponse:
        return RedirectResponse('/demo')

    return app
