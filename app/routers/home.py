"""Главная страница: своя для каждой роли (SPEC §4)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site
from app.services.shifts import open_shift, shift_sites
from app.services.task_labels import assembler_tasks
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_RETURNS, ROLE_STOREKEEPER
from app.timeutil import today_msk
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), err: str = "", ok: str = "") -> Response:
    """Сборщик — сканер, грузчик — смена, кладовщик — печать, возвраты — приёмка, остальные — табло."""
    user = request.state.user
    if user.role in (ROLE_ASSEMBLER, ROLE_LOADER):
        shift = open_shift(db, user.id)
        site = db.get(Site, shift.site_id) if shift else None
        return page(
            request,
            "my_day.html",
            {
                "shift": shift,
                "site": site,
                "sites": shift_sites(db, user),
                "tasks": assembler_tasks(db, user_id=user.id, day=today_msk())
                if user.role == ROLE_ASSEMBLER
                else [],
                "err": err,
                "ok": ok,
            },
        )
    if user.role == ROLE_STOREKEEPER:
        return RedirectResponse("/labels", status_code=303)
    if user.role == ROLE_RETURNS:
        return RedirectResponse("/returns", status_code=303)
    if user.role == ROLE_ADMIN:
        return RedirectResponse("/admin/section/lead", status_code=303)
    return RedirectResponse("/board", status_code=303)
