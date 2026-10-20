"""«Печать по задачам»: кладовщик печатает этикетки на план сборщиков и убирает висящие."""

import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site, Task, Unit
from app.routers.make import labels_pdf_response
from app.services.context import site_and_shift
from app.services.task_labels import (
    article_tasks,
    hanging_units,
    labels_plan,
    print_for_tasks,
    void_units,
)
from app.services.units import UnitError, find_unit
from app.timeutil import today_msk
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _back(day: date, site_id: int, *, ok: str = "", err: str = "") -> RedirectResponse:
    params = {"day": day.isoformat(), "site_id": str(site_id)}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    return RedirectResponse(f"/labels?{urlencode(params)}", status_code=303)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value) if value else today_msk()
    except ValueError:
        return today_msk()


def _scope_tasks(
    db: Session, *, day: date, site_id: int, task_id: int, user_id: int
) -> list[Task]:
    """Задачи, к которым относится действие: одна задача, все у сборщика или все за день."""
    if task_id:
        task = db.get(Task, task_id)
        return [task] if task is not None and task.kind == "article" else []
    return article_tasks(db, day=day, site_id=site_id, user_id=user_id or None)


@router.get("/labels", response_class=HTMLResponse)
def labels_page(
    request: Request,
    db: Session = Depends(get_db),
    day: str = "",
    site_id: int = 0,
    ok: str = "",
    err: str = "",
) -> Response:
    """План печати на день: задачи сборщиков, напечатано, сделано, висит."""
    target_day = _parse_day(day)
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())
    user = request.state.user
    site = db.get(Site, site_id) if site_id else None
    if site is None and not user.home_site_id:
        # Без своей площадки — первая площадка, где на этот день есть задачи
        site = next(
            (s for s in sites if article_tasks(db, day=target_day, site_id=s.id)), None
        )
    if site is None:
        site, _ = site_and_shift(db, user)
    groups = labels_plan(db, day=target_day, site_id=site.id)
    return page(
        request,
        "labels.html",
        {
            "day": target_day,
            "tomorrow": today_msk() + timedelta(days=1),
            "site": site,
            "sites": sites,
            "groups": groups,
            "total_to_print": sum(g.to_print for g in groups),
            "total_hanging": sum(g.hanging for g in groups),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/labels/print")
def labels_print(
    request: Request,
    day: str = Form(""),
    site_id: int = Form(0),
    task_id: int = Form(0),
    user_id: int = Form(0),
    qty: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Напечатать этикетки: на задачу, на сборщика или на весь день — сколько не хватает до плана."""
    user = request.state.user
    target_day = _parse_day(day)
    tasks = _scope_tasks(db, day=target_day, site_id=site_id, task_id=task_id, user_id=user_id)
    try:
        amount = int(qty) if qty.strip() else None
    except ValueError:
        return _back(target_day, site_id, err="Количество — целое число")
    _, shift_id = site_and_shift(db, user)
    try:
        units = print_for_tasks(db, tasks=tasks, actor=user, qty=amount, shift_id=shift_id)
    except UnitError as exc:
        return _back(target_day, site_id, err=str(exc))
    logger.info(
        "Напечатаны этикетки по задачам",
        extra={"user_id": user.id, "unit_number": f"{units[0].number}+{len(units) - 1}"},
    )
    return labels_pdf_response(db, units)


@router.post("/labels/void")
def labels_void(
    request: Request,
    day: str = Form(""),
    site_id: int = Form(0),
    task_id: int = Form(0),
    user_id: int = Form(0),
    db: Session = Depends(get_db),
) -> Response:
    """Убрать висящие этикетки: зеркало не сделано, скан этих этикеток больше не пройдёт."""
    target_day = _parse_day(day)
    tasks = _scope_tasks(db, day=target_day, site_id=site_id, task_id=task_id, user_id=user_id)
    units = hanging_units(db, task_ids=[task.id for task in tasks])
    if not units:
        return _back(target_day, site_id, err="Висящих этикеток нет")
    count = void_units(db, units=units, actor=request.state.user)
    return _back(target_day, site_id, ok=f"Убрано этикеток: {count}")


@router.post("/labels/void-unit")
def labels_void_unit(
    request: Request,
    number: str = Form(...),
    back: str = Form("/warehouse/printed-old"),
    db: Session = Depends(get_db),
) -> Response:
    """Аннулировать одну неиспользованную этикетку (например, из списка «Напечатано больше суток»)."""
    target = back if back.startswith("/") and not back.startswith("//") else "/warehouse/printed-old"
    unit: Unit | None = find_unit(db, number)
    if unit is None or not void_units(db, units=[unit], actor=request.state.user):
        params = {"err": "Убрать можно только этикетку в статусе «Напечатано»"}
    else:
        params = {"ok": f"Этикетка {unit.number} убрана"}
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}{urlencode(params)}", status_code=303)
