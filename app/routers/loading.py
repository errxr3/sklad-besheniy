"""Погрузка на паллету — единственный рабочий экран грузчика (SPEC §6.4)."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Pallet
from app.services.context import site_and_shift
from app.services.shipping import (
    ShippingError,
    close_pallet,
    open_new_pallet,
    open_pallet_for,
    pallet_count,
    pallet_summary,
    scan_loading,
)
from app.settings_store import get_flag
from app.statuses import CHANNELS
from app.web import GuardedRoute, page, read_json

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


@router.get("/loading", response_class=HTMLResponse)
def loading_page(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Экран погрузки: канал, паллета, скан."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    pallet = open_pallet_for(db, user.id)
    return page(
        request,
        "loading.html",
        {
            "site": site,
            "pallet": pallet,
            "channels": CHANNELS,
            "count": pallet_count(db, pallet.id) if pallet else 0,
            "require_mp": get_flag(db, "require_mp_barcode"),
            "err": err,
        },
    )


@router.post("/loading/new-pallet")
def loading_new_pallet(
    request: Request, channel: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    """Открыть новую паллету выбранного канала."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    try:
        pallet = open_new_pallet(db, user=user, site=site, channel=channel)
    except ShippingError as exc:
        return RedirectResponse(f"/loading?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Открыта паллета", extra={"user_id": user.id, "status": pallet.number})
    return RedirectResponse("/loading", status_code=303)


@router.post("/loading/scan")
async def loading_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан зеркала (и, если нужно, ШК маркетплейса) при укладке на паллету."""
    payload = await read_json(request)
    code = str(payload.get("code") or "").strip()
    unit_number = payload.get("unit_number")
    user = request.state.user
    _, shift_id = site_and_shift(db, user)

    pallet = open_pallet_for(db, user.id)
    if pallet is None:
        return JSONResponse({"ok": False, "message": "Сначала откройте паллету"})
    if not code:
        return JSONResponse({"ok": False, "message": "Пустой скан"})

    result = scan_loading(
        db,
        code=code,
        pallet=pallet,
        actor=user,
        shift_id=shift_id,
        unit_number=str(unit_number) if unit_number else None,
    )
    return JSONResponse(
        {
            "ok": result.ok,
            "message": result.message,
            "detail": result.detail,
            "number": result.number,
            "article": result.article,
            "await_mp": result.await_mp,
            "pallet_count": result.pallet_count,
        }
    )


@router.post("/loading/close")
def loading_close(request: Request, pallet_id: int = Form(...), db: Session = Depends(get_db)) -> Response:
    """Закрыть паллету и показать итог."""
    user = request.state.user
    try:
        pallet = close_pallet(db, pallet_id=pallet_id, user=user)
    except ShippingError as exc:
        return RedirectResponse(f"/loading?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Паллета закрыта", extra={"user_id": user.id, "status": pallet.number})
    return RedirectResponse(f"/loading/pallet/{pallet.id}", status_code=303)


@router.get("/loading/pallet/{pallet_id}", response_class=HTMLResponse)
def pallet_card(request: Request, pallet_id: int, db: Session = Depends(get_db)) -> Response:
    """Итог паллеты: количество по артикулам."""
    pallet = db.get(Pallet, pallet_id)
    if pallet is None:
        return page(request, "error.html", {"message": "Паллета не найдена", "status_code": 404})
    return page(
        request,
        "pallet.html",
        {
            "pallet": pallet,
            "rows": pallet_summary(db, pallet.id),
            "total": pallet_count(db, pallet.id),
        },
    )
