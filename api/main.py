"""FastAPI-приложение: uvicorn api.main:app (или python run.py --serve)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from coverage import __version__
from coverage.assistant.groq_client import GroqClient
from coverage.env import load_env
from coverage.errors import ConfigError, DataValidationError
from coverage.field_import import ImportTooLarge
from coverage.render import STATIC

from .assistant_routes import router as assistant_router
from .field_routes import page_router as field_page_router
from .field_routes import router as field_router
from .ratelimit import RateLimiter, RateLimitExceeded
from .routes import create_state, router

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


NO_STORE_PREFIXES = ("/api/field", "/api/mapdata", "/api/assistant")


class NoStoreMiddleware:
    """Cache-Control: no-store на /api/field/* и /api/mapdata: иначе браузер или прокси покажет старые данные."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(NO_STORE_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


def create_app(
    data_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    field_db_path: str | Path | None = None,
    admin_token: str | None = None,
    rate_limit_per_min: int | None = None,
) -> FastAPI:
    data = Path(data_dir or os.environ.get("COVERAGE_DATA_DIR") or ROOT / "data")
    cfg_env = config_path or os.environ.get("COVERAGE_CONFIG")
    cfg = Path(cfg_env) if cfg_env else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Результат по датасету по умолчанию считается один раз на старте и кэшируется.
        app.state.coverage = create_state(
            data, cfg, field_db_path, admin_token or os.environ.get("ADMIN_TOKEN"), rate_limit_per_min
        )
        load_env()
        app.state.chat_limit = RateLimiter(15)
        app.state.groq = GroqClient()
        try:
            yield
        finally:
            app.state.groq.http.close()
        if app.state.coverage.field_store is not None:
            app.state.coverage.field_store.close()

    app = FastAPI(
        title="Карта покрытия",
        version=__version__,
        description="Очистка замеров RSRP, сетка 200 м, выбор двух площадок для новой БС.",
        lifespan=lifespan,
    )

    app.add_middleware(NoStoreMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429, content={"detail": str(exc)}, headers={"Retry-After": str(exc.retry_after)}
        )

    @app.exception_handler(DataValidationError)
    async def _data_error(_: Request, exc: DataValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ImportTooLarge)
    async def _too_large(_: Request, exc: ImportTooLarge) -> JSONResponse:
        return JSONResponse(status_code=413, content={"detail": str(exc)})

    @app.exception_handler(ConfigError)
    async def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    app.include_router(assistant_router)
    app.include_router(router)
    app.include_router(field_router)
    app.include_router(field_page_router)
    return app


app = create_app()
