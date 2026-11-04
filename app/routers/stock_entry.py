"""«Ввод остатков»: печать этикеток сразу на склад и уборка лишних сканом."""

import json
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Article, Site, User
from app.routers.make import labels_pdf_response
from app.services.admin import log_admin
from app.services.legacy_stock import (
    apply_legacy,
    legacy_counts,
    legacy_template_xlsx,
    preview_legacy,
    read_legacy_xlsx,
)
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


# ---------- старый остаток без этикеток (решение владельца 2026-11-03) ----------


def _legacy_page(request: Request, db: Session, *, rows: list | None = None, payload: str = "",
                 site_id: int = 0, ok: str = "", err: str = "") -> Response:
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())
    names = {s.id: s.name for s in sites}
    counts = legacy_counts(db)
    found = db.scalars(select(Article).where(Article.id.in_({a for a, _ in counts}))).all()
    articles = {a.id: a.name for a in found}
    current = sorted(
        (names.get(site, "?"), articles.get(article, "?"), n) for (article, site), n in counts.items()
    )
    return page(
        request,
        "legacy_stock.html",
        {"sites": sites, "rows": rows, "payload": payload, "site_id": site_id, "current": current,
         "total": sum(n for _, _, n in current), "ok": ok, "err": err},
    )


@router.get("/stock-entry/legacy", response_class=HTMLResponse)
def legacy_form(request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "") -> Response:
    """Старый остаток числом: загрузка Excel и сколько старых без этикеток сейчас."""
    return _legacy_page(request, db, ok=ok, err=err)


@router.get("/stock-entry/legacy/template.xlsx")
def legacy_template(request: Request, db: Session = Depends(get_db)) -> Response:
    """Шаблон Excel: все артикулы с площадкой, вписать только количество старых."""
    from app.routers.stock import _attachment

    return Response(
        content=legacy_template_xlsx(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment("Шаблон_старый_остаток.xlsx", "legacy_template.xlsx")},
    )


@router.post("/stock-entry/legacy/preview", response_class=HTMLResponse)
async def legacy_preview(
    request: Request, site_id: int = Form(0), file: UploadFile = File(...), db: Session = Depends(get_db)
) -> Response:
    """Показать, что будет заведено, ничего не записывая."""
    content = await file.read()
    try:
        lines = read_legacy_xlsx(content)
    except UnitError as exc:
        return _legacy_page(request, db, site_id=site_id, err=str(exc))
    rows = preview_legacy(db, lines, default_site=db.get(Site, site_id) if site_id else None)
    payload = json.dumps(lines, ensure_ascii=False)
    return _legacy_page(request, db, rows=rows, payload=payload, site_id=site_id)


@router.post("/stock-entry/legacy/apply")
async def legacy_apply(request: Request, db: Session = Depends(get_db)) -> Response:
    """Завести старые зеркала без этикеток по проверенному файлу (цифры пересчитываются заново)."""
    form = await request.form()
    try:
        lines = [(str(n), str(s), int(q)) for n, s, q in json.loads(str(form.get("payload") or "[]"))]
        site_id = int(str(form.get("site_id") or 0))
    except (ValueError, TypeError):
        return RedirectResponse("/stock-entry/legacy?" + urlencode({"err": "Загрузите файл ещё раз"}), 303)
    user = request.state.user
    rows = preview_legacy(db, lines, default_site=db.get(Site, site_id) if site_id else None)
    created, removed = apply_legacy(db, rows, actor=user)
    log_admin(db, user.id, "старый остаток без этикеток", "units", None, None,
              {"created": created, "written_off": removed})
    logger.info("Старый остаток без этикеток", extra={"user_id": user.id, "status": f"+{created} -{removed}"})
    text = f"Старых без этикеток: добавлено {created}, списано {removed}"
    return RedirectResponse("/stock-entry/legacy?" + urlencode({"ok": text}), 303)
