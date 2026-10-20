"""Ключи API, страница «Состояние» и документация API (SPEC §10, §6)."""

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Shift, Unit
from app.scheduler import scheduler_running
from app.services.admin import log_admin
from app.services.apikeys import create_key, deactivate_key, list_keys
from app.services.sheets import is_configured, last_sync, minutes_since_sync
from app.services.units import printed_older_than
from app.services.warehouse import units_in_transit
from app.statuses import IN_STOCK
from app.timeutil import UTC_FROM_TIMESTAMP, fmt_dt, now_utc
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)

BACKUP_DIR = Path("/backups")


@router.get("/admin/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "", new_key: str = ""
) -> Response:
    """Ключи API для BLAGO OTGRUZKI."""
    return page(
        request,
        "admin/api_keys.html",
        {"keys": list_keys(db), "ok": ok, "err": err, "new_key": new_key},
    )


@router.post("/admin/api-keys/create")
def api_keys_create(request: Request, name: str = Form(...), db: Session = Depends(get_db)) -> Response:
    """Создать ключ: открытое значение показывается один раз."""
    try:
        record, plain = create_key(db, name=name, created_by=request.state.user.id)
    except ValueError as exc:
        return RedirectResponse(f"/admin/api-keys?{urlencode({'err': str(exc)})}", status_code=303)
    log_admin(db, request.state.user.id, "создан ключ API", "api_key", record.id, None, {"name": record.name})
    # Ключ показывается сразу на странице и больше нигде не хранится в открытом виде
    return page(
        request,
        "admin/api_keys.html",
        {"keys": list_keys(db), "ok": f"Ключ «{record.name}» создан", "err": "", "new_key": plain},
    )


@router.post("/admin/api-keys/deactivate")
def api_keys_deactivate(request: Request, key_id: int = Form(...), db: Session = Depends(get_db)) -> Response:
    """Отключить ключ."""
    record = deactivate_key(db, key_id)
    if record is None:
        return RedirectResponse(f"/admin/api-keys?{urlencode({'err': 'Ключ не найден'})}", status_code=303)
    log_admin(db, request.state.user.id, "отключён ключ API", "api_key", record.id)
    return RedirectResponse(f"/admin/api-keys?{urlencode({'ok': 'Ключ отключён'})}", status_code=303)


def latest_backup(directory: Path = BACKUP_DIR) -> tuple[str, str, int | None]:
    """Последний файл бэкапа: имя, время, возраст в часах."""
    if not directory.is_dir():
        return "", "", None
    files = sorted(directory.glob("sklad_*.sql.gz"), key=lambda p: p.stat().st_mtime)
    if not files:
        return "", "", None
    last = files[-1]
    moment = datetime.fromtimestamp(last.stat().st_mtime, tz=UTC_FROM_TIMESTAMP)
    age = int((now_utc() - moment).total_seconds() // 3600)
    return last.name, fmt_dt(moment), age


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Состояние системы: база, планировщик, гугл-таблица, бэкапы, хвосты по складу."""
    checks: list[dict[str, str]] = []

    try:
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar() or "—"
        checks.append({"name": "База данных", "state": "ok", "detail": f"миграция {version}"})
    except Exception as exc:
        checks.append({"name": "База данных", "state": "err", "detail": str(exc)})

    running = scheduler_running()
    checks.append(
        {
            "name": "Планировщик (автозакрытие смен, выгрузка)",
            "state": "ok" if running else "warn",
            "detail": "работает" if running else "выключен (SCHEDULER_ENABLED=0)",
        }
    )

    sync_time, sync_status = last_sync(db)
    minutes = minutes_since_sync(db)
    if not is_configured():
        sheets_state, sheets_detail = "warn", "не настроена (нет ID таблицы или ключа)"
    elif minutes is None:
        sheets_state, sheets_detail = "warn", f"ещё не выгружалась. {sync_status}"
    elif minutes >= 25:
        sheets_state, sheets_detail = "err", f"не обновлялась {minutes} минут. {sync_status}"
    else:
        sheets_state, sheets_detail = "ok", f"обновлена {minutes} мин назад. {sync_status}"
    checks.append({"name": "Гугл-таблица", "state": sheets_state, "detail": sheets_detail})

    backup_name, backup_time, backup_age = latest_backup()
    if backup_age is None:
        checks.append({"name": "Ночной бэкап", "state": "warn", "detail": "файлов бэкапа не найдено"})
    else:
        checks.append(
            {
                "name": "Ночной бэкап",
                "state": "ok" if backup_age <= 30 else "err",
                "detail": f"{backup_name}, {backup_time}",
            }
        )

    open_shifts = db.scalar(select(func.count()).select_from(Shift).where(Shift.ended_at.is_(None))) or 0
    in_stock = db.scalar(select(func.count()).select_from(Unit).where(Unit.status == IN_STOCK)) or 0
    figures = {
        "На складе всего зеркал": int(in_stock),
        "Открытых смен": int(open_shifts),
        "Напечатано больше суток назад": len(printed_older_than(db, hours=24)),
        "В пути больше суток": len(units_in_transit(db)),
    }
    return page(
        request,
        "status.html",
        {"checks": checks, "figures": figures, "now": fmt_dt(now_utc()), "sync_time": sync_time},
    )


@router.get("/openapi.json")
def openapi_schema(request: Request) -> JSONResponse:
    """Схема API — только для администратора (SPEC §10)."""
    return JSONResponse(request.app.openapi())


@router.get("/docs", response_class=HTMLResponse)
def api_docs(request: Request) -> HTMLResponse:
    """Документация API на локальном Swagger UI (без CDN)."""
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>API — BLAGO</title>
<link rel="stylesheet" href="/static/vendor/swagger-ui.css"></head>
<body><div id="swagger"></div>
<script src="/static/vendor/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({url: '/openapi.json', dom_id: '#swagger'});</script>
</body></html>"""
    return HTMLResponse(html)
