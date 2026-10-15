"""Точка входа FastAPI-приложения."""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import get_engine
from app.logging_setup import setup_logging
from app.routers import (
    admin,
    api_v1,
    auth,
    home,
    journal,
    loading,
    make,
    mirror,
    shifts,
    stock,
    system,
    tasks,
    warehouse,
)
from app.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Старт и остановка приложения."""
    setup_logging()
    logger.info("Приложение запускается")
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Приложение остановлено")


def create_app() -> FastAPI:
    """Собрать приложение."""
    settings = get_settings()
    app = FastAPI(
        title="Склад зеркал",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="sklad_session",
        https_only=settings.session_cookie_secure,
        same_site="lax",
        max_age=settings.session_hours * 3600,
    )
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(admin.router)
    app.include_router(make.router)
    app.include_router(mirror.router)
    app.include_router(shifts.router)
    app.include_router(tasks.router)
    app.include_router(loading.router)
    app.include_router(warehouse.router)
    app.include_router(journal.router)
    app.include_router(stock.router)
    app.include_router(api_v1.router)
    app.include_router(system.router)

    @app.get("/health")
    def health() -> JSONResponse:
        """Проверка живости: приложение и база."""
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok", "database": "ok"})
        except Exception:
            logger.exception("Проверка базы не прошла")
            return JSONResponse({"status": "error", "database": "error"}, status_code=503)

    @app.middleware("http")
    async def errors_to_page(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """500 не показывает трассировку: пользователь видит код ошибки (SPEC §11)."""
        try:
            return await call_next(request)
        except Exception:
            error_id = uuid.uuid4().hex[:8]
            logger.exception(
                "Необработанная ошибка",
                extra={"error_id": error_id, "path": request.url.path, "method": request.method},
            )
            html = (
                "<!doctype html><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<body style='font-family:system-ui;font-size:20px;padding:24px'>"
                f"<h1>Ошибка</h1><p>Код {error_id} — сообщите администратору.</p>"
                "<p><a href='/'>На главную</a></p></body>"
            )
            return HTMLResponse(html, status_code=500)

    return app


app = create_app()
