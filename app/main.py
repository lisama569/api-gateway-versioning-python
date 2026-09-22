"""FastAPI application factory."""
from __future__ import annotations

from fastapi import FastAPI

from app.config import Settings
from app.gateway import Gateway
from app.routes import router
from app.versioning import build_default_registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(title="multi-version-api-gateway")
    gateway = Gateway(build_default_registry(), settings)
    gateway.install(application)
    application.include_router(router)
    return application


app = create_app()
