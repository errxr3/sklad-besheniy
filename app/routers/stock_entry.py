"""«Ввод остатков»: печать этикеток сразу на склад и уборка лишних сканом."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site, User
from app.routers.make import labels_pdf_response
from app.services.stock_entry import MAX_ENTRY_QTY, enter_stock, remove_extra
from app.services.units import UnitError
from app.statuses import ROLE_STOREKEEPER
from app.web import GuardedRoute, page, read_json

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _entry_sites(db: Session, user: User) -> list[Site]:
    """Кладовщик заводит остатки только своей площадки, администратор — любой."""
    sites = list(
        db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id)).all()
    )
    if user.role == ROLE_STOREKEEPER:
        return [site for site in sites if site.id == user.home_site_id]
    return sites


@router.get("/stock-entry", response_class=HTMLResponse)
def stock_entry_page(request: Request, db: Session = Depends(get_db), err: str = "") -> Response:
    """Печать этикеток для ввода остатков."""
    sites = _entry_sites(db, request.state.user)
    if not sites:
        return page(
            request,
            "error.html",
            {
                "message": "Не указана ваша площадка — администратор должен выбрать её в карточке сотрудника",
                "status_code": 400,
            },
        )
    return page(
        request, "stock_entry.html", {"sites": sites, "max_qty": MAX_ENTRY_QTY, "err": err}
    )


@router.post("/stock-entry/print")
def stock_entry_print(
    request: Request,
    article_id: int = Form(0),
    qty: int = Form(0),
    site_id: int = Form(0),
    db: Session = Depends(get_db),
) -> Response:
    """Напечатать этикетки и сразу записать зеркала на склад площадки."""
    user = request.state.user
    allowed = {site.id for site in _entry_sites(db, user)}
    try:
        if site_id not in allowed:
            raise UnitError("Выберите свою площадку")
        units = enter_stock(db, article_id=article_id, qty=qty, site_id=site_id, actor=user)
    except UnitError as exc:
        return RedirectResponse(f"/stock-entry?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info(
        "Ввод остатков",
        extra={"user_id": user.id, "unit_number": f"{units[0].number}+{len(units) - 1}"},
    )
    return labels_pdf_response(db, units)


@router.get("/stock-entry/remove", response_class=HTMLResponse)
def stock_entry_remove_page(request: Request) -> Response:
    """Скан лишних этикеток ввода остатков."""
    return page(
        request,
        "scan_op.html",
        {
            "title": "Лишние этикетки",
            "hint": "Сканируйте этикетки, которые не наклеили: после «Ввода остатков», после «Собрал "
            "всё», если сделали меньше, и по задачам, которые не доделали. Они уберутся со склада, "
            "их скан больше не пройдёт. Наклеенные этикетки не трогайте.",
            "url": "/stock-entry/remove/scan",
            "button": "Убрать",
        },
    )


@router.post("/stock-entry/remove/scan")
async def stock_entry_remove_scan(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Скан лишней этикетки."""
    payload = await read_json(request)
    result = remove_extra(db, code=str(payload.get("code") or ""), actor=request.state.user)
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
