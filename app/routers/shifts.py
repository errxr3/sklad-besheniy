"""Смены: начало и окончание кнопкой, табло, табель (SPEC §5; без QR — решение 2026-10-21)."""

import csv
import io
import logging
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Shift, Site, User
from app.qr import build_shift_code_pdf
from app.services.admin import log_admin
from app.services.context import current_site
from app.services.shifts import (
    ShiftError,
    board_rows,
    edit_shift,
    end_shift,
    make_shift_code,
    start_shift,
    timesheet,
)
from app.services.shipping import open_channels
from app.services.tasks import tasks_progress
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER
from app.timeutil import MOSCOW, fmt_dt, msk_day_bounds, now_utc, to_moscow, today_msk
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _client_ip(request: Request) -> str | None:
    """IP устройства с учётом обратного прокси."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/shift/start")
def shift_start_page() -> Response:
    """Смена начинается кнопкой на главной («Сканер» / «Смена»)."""
    return RedirectResponse("/", status_code=303)


@router.get("/shift/end")
def shift_end_page() -> Response:
    """Смена заканчивается кнопкой на главной."""
    return RedirectResponse("/", status_code=303)


@router.post("/shift/start")
def shift_start(request: Request, site_id: int = Form(0), db: Session = Depends(get_db)) -> Response:
    """Начать смену кнопкой на выбранной площадке (без QR-кода)."""
    user = request.state.user
    try:
        start_shift(
            db,
            user=user,
            site_id=site_id,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except ShiftError as exc:
        return RedirectResponse(f"/?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Смена начата", extra={"user_id": user.id})
    return RedirectResponse("/", status_code=303)


@router.post("/shift/end")
def shift_end(request: Request, db: Session = Depends(get_db)) -> Response:
    """Закончить смену кнопкой."""
    user = request.state.user
    try:
        end_shift(db, user=user)
    except ShiftError as exc:
        return RedirectResponse(f"/?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Смена закончена", extra={"user_id": user.id})
    return RedirectResponse("/", status_code=303)


def _default_board_site(db: Session, user: User) -> Site:
    """Площадка табло по умолчанию: своя, иначе та, где сегодня есть смены."""
    if user.home_site_id:
        site = db.get(Site, user.home_site_id)
        if site is not None and site.active:
            return site
    start, end = msk_day_bounds(today_msk())
    busiest = db.execute(
        select(Shift.site_id, func.count())
        .where(Shift.started_at >= start, Shift.started_at < end)
        .group_by(Shift.site_id)
        .order_by(func.count().desc())
        .limit(1)
    ).first()
    if busiest is not None:
        site = db.get(Site, int(busiest[0]))
        if site is not None and site.active:
            return site
    return current_site(db, user)


@router.get("/board", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db), site_id: int = 0, day: str = "") -> Response:
    """Табло «Кто на смене» (SPEC §5.5)."""
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())
    chosen = db.get(Site, site_id) if site_id else _default_board_site(db, request.state.user)
    target_day = date.fromisoformat(day) if day else today_msk()
    rows = board_rows(db, site_id=chosen.id, day=target_day) if chosen else []
    return page(
        request,
        "board.html",
        {
            "sites": sites,
            "site": chosen,
            "day": target_day,
            "rows": rows,
            "open_channels": open_channels(db),
        },
    )


@router.get("/board/data")
def board_data(request: Request, db: Session = Depends(get_db), site_id: int = 0) -> JSONResponse:
    """Данные табло для обновления раз в 30 секунд без перезагрузки."""
    chosen = db.get(Site, site_id) if site_id else current_site(db, request.state.user)
    rows = board_rows(db, site_id=chosen.id, day=today_msk()) if chosen else []
    return JSONResponse(
        {
            "updated": fmt_dt(now_utc()),
            "rows": [
                {
                    "name": row.user.name,
                    "role": row.user.role,
                    "state": row.state,
                    "from": to_moscow(row.shift.started_at).strftime("%H:%M") if row.shift else "",
                    "to": (
                        to_moscow(row.shift.ended_at).strftime("%H:%M")
                        if row.shift and row.shift.ended_at
                        else ""
                    ),
                    "accepted": row.accepted,
                    "shipped": row.shipped,
                    "tasks": f"{row.tasks_done}/{row.tasks_plan}" if row.tasks_plan else "",
                }
                for row in rows
            ],
        }
    )


@router.post("/board/close-shift")
def board_close_shift(
    request: Request,
    shift_id: int = Form(...),
    ended_at: str = Form(""),
    note: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Начальник смены закрывает или правит забытую смену с комментарием."""
    from urllib.parse import urlencode

    moment = now_utc()
    if ended_at.strip():
        naive = datetime.fromisoformat(ended_at)
        moment = naive.replace(tzinfo=MOSCOW)
    try:
        edit_shift(db, shift_id=shift_id, actor=request.state.user, ended_at=moment, note=note)
    except ShiftError as exc:
        return RedirectResponse(f"/board?{urlencode({'err': str(exc)})}", status_code=303)
    return RedirectResponse("/board", status_code=303)


@router.get("/reports/timesheet", response_class=HTMLResponse)
def timesheet_page(
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> Response:
    """Табель за период (SPEC §5.6)."""
    today = today_msk()
    start = date.fromisoformat(date_from) if date_from else today.replace(day=1)
    end = date.fromisoformat(date_to) if date_to else today
    users = list(
        db.scalars(
            select(User)
            .where(User.active.is_(True), User.role.in_((ROLE_ASSEMBLER, ROLE_LOADER)))
            .order_by(User.name)
        ).all()
    )
    chosen = db.get(User, user_id) if user_id else (users[0] if users else None)
    rows = timesheet(db, user_id=chosen.id, date_from=start, date_to=end) if chosen else []
    return page(
        request,
        "timesheet.html",
        {
            "users": users,
            "chosen": chosen,
            "date_from": start,
            "date_to": end,
            "rows": rows,
            "total_hours": round(sum(row.hours for row in rows), 2),
        },
    )


@router.get("/reports/timesheet.csv")
def timesheet_csv(
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> Response:
    """Табель в CSV для Excel: UTF-8 с BOM, разделитель «;» (SPEC §5.6)."""
    today = today_msk()
    start = date.fromisoformat(date_from) if date_from else today.replace(day=1)
    end = date.fromisoformat(date_to) if date_to else today
    user = db.get(User, user_id)
    if user is None:
        return page(request, "error.html", {"message": "Сотрудник не найден", "status_code": 404})
    rows = timesheet(db, user_id=user.id, date_from=start, date_to=end)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Дата", "Начало", "Окончание", "Часы", "Как закрыта"])
    for row in rows:
        writer.writerow(
            [
                row.day.strftime("%d.%m.%Y"),
                to_moscow(row.started_at).strftime("%H:%M"),
                to_moscow(row.ended_at).strftime("%H:%M") if row.ended_at else "",
                f"{row.hours:.2f}".replace(".", ","),
                row.closed_label,
            ]
        )
    writer.writerow(["Итого", "", "", f"{sum(r.hours for r in rows):.2f}".replace(".", ","), ""])
    data = "﻿" + buffer.getvalue()
    filename = f"tabel_{user.id}_{start:%Y-%m-%d}_{end:%Y-%m-%d}.csv"
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/admin/sites/{site_id}/rotate-code")
def rotate_code(request: Request, site_id: int, db: Session = Depends(get_db)) -> Response:
    """Перевыпустить код смены площадки: старый сразу перестаёт работать."""
    site = db.get(Site, site_id)
    if site is None:
        return page(request, "error.html", {"message": "Площадка не найдена", "status_code": 404})
    make_shift_code(db, site)
    log_admin(db, request.state.user.id, "перевыпущен код смены", "site", site.id)
    logger.info("Перевыпущен код смены", extra={"user_id": request.state.user.id})
    return RedirectResponse(f"/admin/sites/{site.id}/code.pdf", status_code=303)


@router.get("/admin/sites/{site_id}/code.pdf")
def shift_code_pdf(request: Request, site_id: int, db: Session = Depends(get_db)) -> Response:
    """Лист A4 с QR-кодом смены. Код выпускается, если его ещё не было."""
    site = db.get(Site, site_id)
    if site is None:
        return page(request, "error.html", {"message": "Площадка не найдена", "status_code": 404})
    code = make_shift_code(db, site)
    log_admin(db, request.state.user.id, "напечатан код смены", "site", site.id)
    pdf = build_shift_code_pdf(site.name, code, fmt_dt(site.shift_code_rotated_at))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="shift_code_{site.id}.pdf"'},
    )


@router.get("/warehouse/open-shifts", response_class=HTMLResponse)
def open_shifts(request: Request, db: Session = Depends(get_db)) -> Response:
    """Незакрытые смены — чтобы начальник смены мог их закрыть."""
    shifts = list(
        db.scalars(select(Shift).where(Shift.ended_at.is_(None)).order_by(Shift.started_at)).all()
    )
    stale = [s for s in shifts if s.started_at < now_utc() - timedelta(hours=16)]
    return page(request, "open_shifts.html", {"shifts": shifts, "stale_ids": {s.id for s in stale}})


@router.get("/my-day/data")
def my_day_data(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Данные «Моего дня» для обновления прогресса без перезагрузки."""
    user = request.state.user
    progress = tasks_progress(db, user_id=user.id, day=today_msk())
    return JSONResponse(
        {
            "plan": progress.plan,
            "done": progress.done,
            "tasks": [
                {
                    "id": view.task.id,
                    "kind": view.task.kind,
                    "name": view.task.article.name if view.task.article else (view.task.text or ""),
                    "plan": view.plan,
                    "done": view.done,
                    "percent": view.percent,
                    "is_done": view.is_done,
                }
                for view in progress.views
            ],
        }
    )
