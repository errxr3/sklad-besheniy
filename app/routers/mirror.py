"""Карточка зеркала: история, смена сборщика, списание (SPEC §6.8)."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services.context import site_and_shift
from app.services.units import (
    UnitError,
    change_assembler,
    find_unit,
    unit_history,
    write_off_unit,
)
from app.statuses import PRINTED, ROLE_ASSEMBLER, WRITE_OFF_REASONS
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


@router.get("/mirror", response_class=HTMLResponse)
def mirror_search(request: Request, db: Session = Depends(get_db), code: str = "") -> Response:
    """Поиск зеркала по номеру или ШК маркетплейса."""
    if code.strip():
        unit = find_unit(db, code)
        if unit is not None:
            return RedirectResponse(f"/mirror/{unit.number}", status_code=303)
        return page(request, "mirror_search.html", {"err": f"Номер {code.strip()} не найден"})
    return page(request, "mirror_search.html", {"err": ""})


@router.get("/mirror/{number}", response_class=HTMLResponse)
def mirror_card(
    request: Request, number: str, db: Session = Depends(get_db), ok: str = "", err: str = ""
) -> Response:
    """Карточка зеркала."""
    unit = find_unit(db, number)
    if unit is None:
        return page(
            request,
            "error.html",
            {"message": f"Зеркало {number} не найдено", "status_code": 404},
        )
    assemblers = db.scalars(
        select(User).where(User.active.is_(True), User.role == ROLE_ASSEMBLER).order_by(User.name)
    ).all()
    return page(
        request,
        "mirror.html",
        {
            "unit": unit,
            "movements": unit_history(db, unit.id),
            "assemblers": assemblers,
            "reasons": WRITE_OFF_REASONS,
            "can_reprint": unit.status == PRINTED,
            "ok": ok,
            "err": err,
        },
    )


@router.post("/mirror/{number}/assembler")
def mirror_set_assembler(
    request: Request,
    number: str,
    assembler_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Сменить сборщика у зеркала (начальник смены и админ)."""
    unit = find_unit(db, number)
    if unit is None:
        return page(request, "error.html", {"message": "Зеркало не найдено", "status_code": 404})
    try:
        change_assembler(db, unit_id=unit.id, assembler_id=assembler_id, actor=request.state.user)
    except UnitError as exc:
        return RedirectResponse(f"/mirror/{unit.number}?{urlencode({'err': str(exc)})}", status_code=303)
    return RedirectResponse(
        f"/mirror/{unit.number}?{urlencode({'ok': 'Сборщик изменён'})}", status_code=303
    )


@router.post("/mirror/{number}/writeoff")
def mirror_write_off(
    request: Request,
    number: str,
    reason: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Списать зеркало (начальник смены и админ)."""
    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    result = write_off_unit(db, code=number, actor=user, reason=reason, note=note, shift_id=shift_id)
    params = {"ok": f"{result.message} · {result.detail}"} if result.ok else {"err": result.message}
    return RedirectResponse(f"/mirror/{number.upper()}?{urlencode(params)}", status_code=303)
