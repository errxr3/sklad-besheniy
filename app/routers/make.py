"""«Сделал зеркало»: печать этикеток и приёмка сканом (SPEC §6.3)."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.labels import LabelData, build_labels_pdf
from app.models import Article, Unit, User
from app.services.context import site_and_shift
from app.services.units import (
    UnitError,
    accept_unit,
    create_printed_units,
    label_rows,
    printed_older_than,
)
from app.settings_store import get_int
from app.statuses import PRINTED, ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER
from app.web import GuardedRoute, page, read_json

# Кто печатает этикетки на любого сборщика
PRINTER_ROLES = (ROLE_ADMIN, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER)

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _assemblers(db: Session) -> list[User]:
    """Активные сборщики, включая служебного «Остаток на запуск»."""
    return list(
        db.scalars(
            select(User)
            .where(User.active.is_(True), User.role == ROLE_ASSEMBLER)
            .order_by(User.is_service, User.name)
        ).all()
    )


@router.get("/make", response_class=HTMLResponse)
def make_page(
    request: Request,
    db: Session = Depends(get_db),
    article_id: int = 0,
    err: str = "",
) -> Response:
    """Экран изготовления: выбор артикула, печать и приёмка."""
    user = request.state.user
    site, _ = site_and_shift(db, user)
    chosen = db.get(Article, article_id) if article_id else None
    return page(
        request,
        "make.html",
        {
            "site": site,
            "chosen": chosen,
            "assemblers": _assemblers(db) if user.role in PRINTER_ROLES else [],
            "can_choose_assembler": user.role in PRINTER_ROLES,
            # Приёмку сканом делают сборщики (у себя в «Мой день»), начальник смены и админ
            "can_accept": user.role in (ROLE_ADMIN, ROLE_SHIFT_LEAD),
            "err": err,
        },
    )


@router.get("/api/articles")
def api_articles(request: Request, db: Session = Depends(get_db), q: str = "") -> JSONResponse:
    """Поиск артикула по части названия (для подсказки на экране)."""
    from app.articles import normalize_article

    query = select(Article).where(Article.kind == "article", Article.active.is_(True))
    text = q.strip()
    if text:
        needle = f"%{normalize_article(text)}%"
        query = query.where(Article.name_norm.like(needle))
    rows = db.scalars(query.order_by(Article.sort_order).limit(30)).all()
    return JSONResponse(
        {"articles": [{"id": a.id, "name": a.name, "group": a.group} for a in rows]}
    )


@router.post("/make/print")
def make_print(
    request: Request,
    article_id: int = Form(...),
    qty: int = Form(...),
    assembler_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Создать зеркала в статусе «Напечатано» и отдать PDF с этикетками."""
    user = request.state.user
    site, shift_id = site_and_shift(db, user)

    target_assembler = user.id
    if user.role in PRINTER_ROLES and assembler_id:
        target_assembler = int(assembler_id)

    try:
        units = create_printed_units(
            db,
            article_id=article_id,
            qty=qty,
            site_id=site.id,
            assembler_id=target_assembler,
            actor_id=user.id,
            shift_id=shift_id,
        )
    except UnitError as exc:
        from urllib.parse import urlencode

        return RedirectResponse(f"/make?{urlencode({'err': str(exc)})}", status_code=303)

    logger.info(
        "Напечатаны этикетки",
        extra={"user_id": user.id, "unit_number": f"{units[0].number}+{len(units) - 1}"},
    )
    return labels_pdf_response(db, units)


def labels_pdf_response(db: Session, units: list[Unit]) -> Response:
    """PDF с этикетками в размере из настроек — открывается во вкладке для печати."""
    width = get_int(db, "label_width_mm")
    height = get_int(db, "label_height_mm")
    pdf = build_labels_pdf(
        [LabelData(*row) for row in label_rows(units)], width_mm=width, height_mm=height
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="labels_{units[0].number}.pdf"'},
    )


@router.post("/scan/accept")
async def scan_accept(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Приёмка отсканированной этикетки: «Напечатано» → «На складе»."""
    payload = await read_json(request)
    code = str(payload.get("code") or "").strip()
    if not code:
        return JSONResponse({"ok": False, "message": "Пустой скан"}, status_code=400)
    user = request.state.user
    site, shift_id = site_and_shift(db, user)
    result = accept_unit(db, code=code, actor=user, shift_id=shift_id, site_id=site.id)
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


@router.get("/make/reprint/{number}")
def reprint_label(request: Request, number: str, db: Session = Depends(get_db)) -> Response:
    """Перепечатать этикетку зеркала в статусе «Напечатано» — тот же номер."""
    user = request.state.user
    unit = db.scalar(select(Unit).where(Unit.number == number.strip().upper()))
    if unit is None:
        return page(request, "error.html", {"message": "Номер не найден", "status_code": 404})
    if unit.status != PRINTED:
        return page(
            request,
            "error.html",
            {"message": "Перепечатать можно только этикетку в статусе «Напечатано»", "status_code": 400},
        )
    if user.role == ROLE_ASSEMBLER and unit.assembler_id != user.id:
        return page(
            request, "error.html", {"message": "Это зеркало другого сборщика", "status_code": 403}
        )

    pdf = build_labels_pdf(
        [LabelData(*row) for row in label_rows([unit])],
        width_mm=get_int(db, "label_width_mm"),
        height_mm=get_int(db, "label_height_mm"),
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="label_{unit.number}.pdf"'},
    )


@router.get("/warehouse/printed-old", response_class=HTMLResponse)
def printed_old(
    request: Request, db: Session = Depends(get_db), ok: str = "", err: str = ""
) -> Response:
    """Этикетки «Напечатано» старше суток: принять или убрать (SPEC §6.3)."""
    units = printed_older_than(db, hours=24)
    return page(request, "printed_old.html", {"units": units, "ok": ok, "err": err})
