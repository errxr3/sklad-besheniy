"""Погрузка по каналам: канал → скан зеркал → закрыть канал с описанием (SPEC §6.4)."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Pallet
from app.services.context import site_and_shift
from app.services.shipping import (
    CHANNEL_BUTTONS,
    ShippingError,
    check_channel_site,
    close_pallet,
    join_channel,
    pallet_count,
    pallet_loaders,
    pallet_summary,
    scan_loading,
    site_channels,
)
from app.settings_store import get_flag
from app.statuses import OZON, OZON_TRUCKS, ROLE_ADMIN
from app.web import GuardedRoute, page, read_json

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


@router.get("/loading", response_class=HTMLResponse)
def loading_page(
    request: Request, db: Session = Depends(get_db), pallet_id: int = 0, err: str = ""
) -> Response:
    """Экран погрузки: каналы площадки (общие для всех грузчиков), скан, закрытие."""
    user = request.state.user
    site, shift_id = site_and_shift(db, user)
    no_shift = shift_id is None and user.role != ROLE_ADMIN
    pallet = db.get(Pallet, pallet_id) if pallet_id and not no_shift else None
    if pallet is not None:
        try:
            check_channel_site(pallet, site, user)
        except ShippingError as exc:
            err, pallet = str(exc), None
    channels = site_channels(db, site.id) if site is not None else {}
    return page(
        request,
        "loading.html",
        {
            "site": site,
            "pallet": pallet,
            "channels": CHANNEL_BUTTONS,
            "open": channels,
            "counts": {name: pallet_count(db, p.id) for name, p in channels.items()},
            "loaders": pallet_loaders(db, pallet.id) if pallet else [],
            "ozon": OZON,
            "ozon_trucks": OZON_TRUCKS,
            "count": pallet_count(db, pallet.id) if pallet else 0,
            "require_mp": get_flag(db, "require_mp_barcode"),
            # Без смены грузить нельзя — вместо каналов кнопка «начать смену»
            "no_shift": no_shift,
            "err": err,
        },
    )


@router.post("/loading/new-pallet")
def loading_new_pallet(
    request: Request, channel: str = Form(...), truck: str = Form(""), db: Session = Depends(get_db)
) -> Response:
    """Встать на канал площадки: открыть его или присоединиться к уже открытому."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    try:
        pallet = join_channel(db, user=user, site=site, channel=channel, truck=truck)
    except ShippingError as exc:
        return RedirectResponse(f"/loading?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Грузчик на канале", extra={"user_id": user.id, "status": pallet.number})
    return RedirectResponse(f"/loading?pallet_id={pallet.id}", status_code=303)


@router.post("/loading/scan")
async def loading_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан зеркала (и, если нужно, ШК маркетплейса) в общий канал площадки."""
    payload = await read_json(request)
    code = str(payload.get("code") or "").strip()
    unit_number = payload.get("unit_number")
    user = request.state.user
    site, shift_id = site_and_shift(db, user)

    try:
        pallet_id = int(payload.get("pallet_id") or 0)
    except (TypeError, ValueError):
        pallet_id = 0
    # FOR SHARE: сканировать могут все сразу, а закрытие канала дождётся идущих сканов
    pallet = db.scalar(select(Pallet).where(Pallet.id == pallet_id).with_for_update(read=True, of=Pallet))
    if pallet is None:
        return JSONResponse({"ok": False, "message": "Сначала выберите канал"})
    try:
        check_channel_site(pallet, site, user)
    except ShippingError as exc:
        return JSONResponse({"ok": False, "message": str(exc), "closed": pallet.closed_at is not None})
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
def loading_close(
    request: Request,
    pallet_id: int = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Закрыть канал (всё отсканировано, готово к отгрузке) и показать итог."""
    user = request.state.user
    try:
        site, _ = site_and_shift(db, user)
        pallet = close_pallet(db, pallet_id=pallet_id, user=user, note=note, site=site)
    except ShippingError as exc:
        return RedirectResponse(f"/loading?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Канал закрыт", extra={"user_id": user.id, "status": pallet.number})
    return RedirectResponse(f"/loading/pallet/{pallet.id}", status_code=303)


@router.get("/loading/pallet/{pallet_id}", response_class=HTMLResponse)
def pallet_card(request: Request, pallet_id: int, db: Session = Depends(get_db)) -> Response:
    """Итог отгрузки канала: количество по артикулам."""
    pallet = db.get(Pallet, pallet_id)
    if pallet is None:
        return page(request, "error.html", {"message": "Отгрузка не найдена", "status_code": 404})
    return page(
        request,
        "pallet.html",
        {
            "pallet": pallet,
            "rows": pallet_summary(db, pallet.id),
            "total": pallet_count(db, pallet.id),
        },
    )
