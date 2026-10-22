"""Складские операции: перемещения (грузчик), возвраты, инвентаризация и брак (админ)."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Inventory, Site
from app.routers.make import labels_pdf_response
from app.services.context import site_and_shift
from app.services.shipping import cancel_ship
from app.services.units import ScanResult, UnitError, write_off_unit
from app.services.warehouse import (
    WarehouseError,
    active_inventory,
    finish_inventory,
    inventory_diff,
    print_unmarked_returns,
    returns_site,
    scan_inventory,
    scan_return,
    start_inventory,
    transfer_in,
    transfer_out,
    units_in_transit,
)
from app.statuses import WRITE_OFF_REASONS
from app.web import GuardedRoute, page, read_json

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _json(result: ScanResult) -> JSONResponse:
    """Ответ скана в JSON."""
    return JSONResponse(
        {
            "ok": result.ok,
            "message": result.message,
            "detail": result.detail,
            "number": result.number,
            "article": result.article,
            "stock": result.stock,
        }
    )


def _sites(db: Session) -> list[Site]:
    """Активные площадки по порядку."""
    return list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())


@router.get("/warehouse", response_class=HTMLResponse)
def warehouse_home(request: Request, db: Session = Depends(get_db)) -> Response:
    """Меню складских операций."""
    return page(request, "warehouse.html", {"in_transit": len(units_in_transit(db))})


# --- Перемещение между площадками ---


@router.get("/transfer", response_class=HTMLResponse)
def transfer_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Отправка зеркал на другую площадку."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    others = [s for s in _sites(db) if s.id != site.id]
    if not others:
        return page(
            request,
            "scan_op.html",
            {"title": "Перемещение", "blocked": "Нет второй активной площадки", "url": "/transfer/scan"},
        )
    return page(
        request,
        "scan_op.html",
        {
            "title": "Перемещение",
            "hint": f"Отправка с площадки {site.name}. Сканируйте зеркала, которые уезжают.",
            "url": "/transfer/scan",
            "button": "Отправить",
            "fields": [
                {
                    "name": "to_site_id",
                    "label": "Куда везём",
                    "type": "select",
                    "options": [(str(s.id), s.name) for s in others],
                }
            ],
        },
    )


@router.post("/transfer/scan")
async def transfer_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан при отправке на другую площадку."""
    payload = await read_json(request)
    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    try:
        to_site_id = int(payload.get("to_site_id") or 0)
    except (TypeError, ValueError):
        to_site_id = 0
    if not to_site_id:
        return JSONResponse({"ok": False, "message": "Выберите площадку-получателя"})
    result = transfer_out(
        db,
        code=str(payload.get("code") or ""),
        actor=user,
        to_site_id=to_site_id,
        shift_id=shift_id,
    )
    return _json(result)


@router.get("/transfer/in", response_class=HTMLResponse)
def transfer_in_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Приёмка перемещения на площадке-получателе."""
    site, _ = site_and_shift(db, request.state.user)
    return page(
        request,
        "scan_op.html",
        {
            "title": "Принять перемещение",
            "hint": f"Приём на площадке {site.name}. Сканируйте приехавшие зеркала.",
            "url": "/transfer/in/scan",
            "button": "Принять",
        },
    )


@router.post("/transfer/in/scan")
async def transfer_in_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан при приёмке перемещения."""
    payload = await read_json(request)
    user = request.state.user
    site, shift_id = site_and_shift(db, user)
    result = transfer_in(
        db, code=str(payload.get("code") or ""), actor=user, site_id=site.id, shift_id=shift_id
    )
    return _json(result)


@router.get("/warehouse/in-transit", response_class=HTMLResponse)
def in_transit_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Зеркала, которые в пути дольше суток (SPEC §6.5)."""
    return page(request, "in_transit.html", {"units": units_in_transit(db)})


# --- Списание брака ---


@router.get("/writeoff", response_class=HTMLResponse)
def writeoff_page(request: Request) -> Response:
    """Списание брака, боя, потери."""
    return page(
        request,
        "scan_op.html",
        {
            "title": "Списание",
            "hint": "Выберите причину и сканируйте зеркала.",
            "url": "/writeoff/scan",
            "button": "Списать",
            "fields": [
                {
                    "name": "reason",
                    "label": "Причина",
                    "type": "select",
                    "options": [(r, r) for r in WRITE_OFF_REASONS],
                },
                {"name": "note", "label": "Комментарий", "type": "text", "placeholder": "не обязательно"},
            ],
        },
    )


@router.post("/writeoff/scan")
async def writeoff_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан при списании."""
    payload = await read_json(request)
    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    reason = str(payload.get("reason") or "брак")
    result = write_off_unit(
        db,
        code=str(payload.get("code") or ""),
        actor=user,
        reason=reason,
        note=str(payload.get("note") or ""),
        shift_id=shift_id,
    )
    return _json(result)


# --- Возвраты ---


@router.get("/returns", response_class=HTMLResponse)
def returns_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Возвраты: скан — и зеркало на складе площадки возвратов (Коминтерна)."""
    site = returns_site(db)
    return page(
        request,
        "scan_op.html",
        {
            "title": "Возвраты",
            "hint": f"Скан этикетки BLAGO — зеркало записывается на склад {site.name}. "
            "Негодное не сканируйте. Зеркало без нашей этикетки: «Этикетки без ШК».",
            "url": "/returns/scan",
            "button": "Принять возврат",
        },
    )


@router.post("/returns/scan")
async def returns_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан возврата: отгруженное зеркало — на склад возвратов."""
    payload = await read_json(request)
    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    return _json(scan_return(db, code=str(payload.get("code") or ""), actor=user, shift_id=shift_id))


@router.get("/returns/print", response_class=HTMLResponse)
def returns_print_page(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Печать ШК для возврата без нашей этикетки."""
    return page(request, "returns_print.html", {"site": returns_site(db), "err": err})


@router.post("/returns/print")
def returns_print(
    request: Request,
    article_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Выдать номера, поставить зеркала на склад возвратов и отдать PDF этикеток."""
    try:
        units = print_unmarked_returns(db, article_id=article_id, qty=qty, actor=request.state.user)
    except (UnitError, WarehouseError) as exc:
        return RedirectResponse(f"/returns/print?{urlencode({'err': str(exc)})}", status_code=303)
    return labels_pdf_response(db, units)


# --- Отмена ошибочной отгрузки ---


@router.get("/ship-cancel", response_class=HTMLResponse)
def ship_cancel_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Отмена ошибочной отгрузки (ограничена по времени)."""
    from app.settings_store import get_int

    minutes = get_int(db, "ship_cancel_minutes")
    return page(
        request,
        "scan_op.html",
        {
            "title": "Отмена отгрузки",
            "hint": f"Отменить можно в течение {minutes} минут после отгрузки. Зеркало вернётся на склад.",
            "url": "/ship-cancel/scan",
            "button": "Отменить отгрузку",
        },
    )


@router.post("/ship-cancel/scan")
async def ship_cancel_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан при отмене отгрузки."""
    payload = await read_json(request)
    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    result = cancel_ship(db, number=str(payload.get("code") or ""), actor=user, shift_id=shift_id)
    return _json(result)


# --- Инвентаризация ---


@router.get("/inventory", response_class=HTMLResponse)
def inventory_home(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Начало инвентаризации или переход к идущей."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    current = active_inventory(db, site.id)
    history = list(
        db.scalars(
            select(Inventory)
            .where(Inventory.site_id == site.id, Inventory.finished_at.is_not(None))
            .order_by(Inventory.started_at.desc())
            .limit(10)
        ).all()
    )
    return page(
        request,
        "inventory_home.html",
        {"site": site, "current": current, "history": history, "err": err},
    )


@router.post("/inventory/start")
def inventory_start(request: Request, db: Session = Depends(get_db)) -> Response:
    """Начать инвентаризацию всей площадки."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    try:
        inventory = start_inventory(db, site_id=site.id, actor=user)
    except WarehouseError as exc:
        return RedirectResponse(f"/inventory?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Начата инвентаризация", extra={"user_id": user.id})
    return RedirectResponse(f"/inventory/{inventory.id}", status_code=303)


@router.get("/inventory/{inventory_id}", response_class=HTMLResponse)
def inventory_page(request: Request, inventory_id: int, db: Session = Depends(get_db)) -> Response:
    """Экран инвентаризации: скан и расхождения."""
    inventory = db.get(Inventory, inventory_id)
    if inventory is None:
        return page(request, "error.html", {"message": "Инвентаризация не найдена", "status_code": 404})
    diff = inventory_diff(db, inventory)
    site = db.get(Site, inventory.site_id)
    return page(
        request,
        "inventory.html",
        {
            "inventory": inventory,
            "site": site,
            "diff": diff,
            "url": f"/inventory/{inventory.id}/scan",
            "finished": inventory.finished_at is not None,
        },
    )


@router.post("/inventory/{inventory_id}/scan")
async def inventory_scan(
    request: Request, inventory_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    """Скан зеркала при пересчёте."""
    payload = await read_json(request)
    result = scan_inventory(
        db,
        inventory_id=inventory_id,
        code=str(payload.get("code") or ""),
        actor=request.state.user,
    )
    return _json(result)


@router.post("/inventory/{inventory_id}/finish")
def inventory_finish(request: Request, inventory_id: int, db: Session = Depends(get_db)) -> Response:
    """Подтвердить инвентаризацию: списать ненайденное, оприходовать лишнее."""
    try:
        written_off, restored = finish_inventory(
            db, inventory_id=inventory_id, actor=request.state.user
        )
    except WarehouseError as exc:
        return RedirectResponse(f"/inventory?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info(
        "Инвентаризация завершена",
        extra={"user_id": request.state.user.id, "status": f"списано {written_off}, найдено {restored}"},
    )
    return RedirectResponse(f"/inventory/{inventory_id}", status_code=303)
