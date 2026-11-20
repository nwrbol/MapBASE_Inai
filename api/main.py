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
from coverage.errors import ConfigError, DataValidationError
from coverage.field_import import ImportTooLarge
from coverage.render import STATIC

from .field_routes import page_router as field_page_router
from .field_routes import router as field_router
from .routes import create_state, router

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def create_app(
    data_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    field_db_path: str | Path | None = None,
) -> FastAPI:
    data = Path(data_dir or os.environ.get("COVERAGE_DATA_DIR") or ROOT / "data")
    cfg_env = config_path or os.environ.get("COVERAGE_CONFIG")
    cfg = Path(cfg_env) if cfg_env else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Результат по датасету по умолчанию считается один раз на старте и кэшируется.
        app.state.coverage = create_state(data, cfg, field_db_path)
        yield
        if app.state.coverage.field_store is not None:
            app.state.coverage.field_store.close()

    app = FastAPI(
        title="Карта покрытия — ТЗ 7",
        version=__version__,
        description="Очистка замеров RSRP, сетка 200 м, выбор двух площадок для новой БС.",
        lifespan=lifespan,
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
    app.include_router(router)
    app.include_router(field_router)
    app.include_router(field_page_router)
    return app


app = create_app()
