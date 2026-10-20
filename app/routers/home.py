"""Главная страница: своя для каждой роли (SPEC §4)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site
from app.services.shifts import open_shift
from app.services.tasks import tasks_progress
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_STOREKEEPER
from app.timeutil import today_msk
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Сборщик и грузчик — «Мой день», кладовщик — «Печать по задачам», остальные — «Табло смены»."""
    user = request.state.user
    if user.role in (ROLE_ASSEMBLER, ROLE_LOADER):
        shift = open_shift(db, user.id)
        site = db.get(Site, shift.site_id) if shift else None
        progress = tasks_progress(db, user_id=user.id, day=today_msk())
        return page(
            request,
            "my_day.html",
            {"shift": shift, "site": site, "progress": progress, "err": err},
        )
    if user.role == ROLE_STOREKEEPER:
        return RedirectResponse("/labels", status_code=303)
    return RedirectResponse("/board", status_code=303)
