"""FastAPI entrypoint: ``uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, get_settings
from .harness_service import HarnessService, ServiceError
from .routes import router
from .session_routes import router as session_router
from .session_store import SessionStore
from .sessions import SessionManager

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("app").setLevel(level.upper())


def create_app(settings: Optional[Settings] = None, service: Optional[HarnessService] = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _setup_logging(settings.log_level)
        harness_service = service or HarnessService(settings)
        app.state.harness_service = harness_service
        app.state.session_manager = SessionManager(SessionStore(settings.home_dir), harness_service)
        logger.info(
            "DeepSeek Harness service ready profile=%s model=%s workspace=%s auth=%s session_log_upload=%s",
            settings.dsh_profile,
            settings.dsh_model,
            settings.workspace_dir,
            "on" if settings.service_api_keys else "off",
            "off" if settings.disable_session_log_upload else "on",
        )
        try:
            yield
        finally:
            await harness_service.close()

    app = FastAPI(title="DeepSeek Harness Service", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})

    app.include_router(router)
    app.include_router(session_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    # The harness lives in process memory: always a single worker.
    # Without a graceful-shutdown timeout, a long agent run would block shutdown indefinitely.
    uvicorn.run("app.main:app", host=_settings.host, port=_settings.port, workers=1, timeout_graceful_shutdown=10)
