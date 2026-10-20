"""Главная страница: своя для каждой роли (SPEC §4)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site
from app.services.shifts import open_shift
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_RETURNS, ROLE_STOREKEEPER
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Сборщик — сканер, грузчик — смена, кладовщик — печать, возвраты — приёмка, остальные — табло."""
    user = request.state.user
    if user.role in (ROLE_ASSEMBLER, ROLE_LOADER):
        shift = open_shift(db, user.id)
        site = db.get(Site, shift.site_id) if shift else None
        return page(
            request,
            "my_day.html",
            {"shift": shift, "site": site, "err": err},
        )
    if user.role == ROLE_STOREKEEPER:
        return RedirectResponse("/labels", status_code=303)
    if user.role == ROLE_RETURNS:
        return RedirectResponse("/returns", status_code=303)
    if user.role == ROLE_ADMIN:
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/board", status_code=303)
