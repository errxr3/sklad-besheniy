"""Приёмка сканом, перепечатка этикетки и PDF этикеток (SPEC §6.3).

Экрана «Сделал зеркало» больше нет: этикетки печатаются только по задачам
(«Печать по задачам») и возвратами для зеркал без маркировки (решение владельца 2026-10-20).
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.labels import LabelData, build_labels_pdf
from app.models import Article, Site, Unit
from app.services.context import site_and_shift
from app.services.units import (
    accept_unit,
    label_rows,
    printed_older_than,
)
from app.settings_store import get_int
from app.statuses import PRINTED, ROLE_ASSEMBLER
from app.web import GuardedRoute, page, read_json

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


@router.get("/api/articles")
def api_articles(
    request: Request, db: Session = Depends(get_db), q: str = "", site_id: int = 0
) -> JSONResponse:
    """Поиск артикула по части названия (для подсказки на экране).

    С `site_id` — только артикулы, которые делают на этой площадке (для задач).
    """
    from sqlalchemy import case, or_

    from app.articles import normalize_article

    query = select(Article).where(Article.kind == "article", Article.active.is_(True))
    text = q.strip()
    first: list = []
    if text:
        # Каждое слово ищется отдельно («100 ambient» тоже найдёт), а сначала идут названия,
        # которые начинаются с набранного: «Ambient 100» → «Ambient 100 krug», а не «Ambient 200x100»
        for word in text.split():
            query = query.where(Article.name_norm.like(f"%{normalize_article(word)}%"))
        first = [case((Article.name_norm.like(f"{normalize_article(text)}%"), 0), else_=1)]
    site = db.get(Site, site_id) if site_id else None
    if site is not None and site.prefixes:
        query = query.where(or_(*[Article.name.ilike(f"{prefix}%") for prefix in site.prefixes]))
    rows = db.scalars(query.order_by(*first, Article.sort_order).limit(40)).all()
    return JSONResponse(
        {"articles": [{"id": a.id, "name": a.name, "group": a.group} for a in rows]}
    )


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


@router.get("/scanner", response_class=HTMLResponse)
def scanner_page(request: Request) -> Response:
    """Сканер приёмки для администратора — тот же скан, что у сборщика."""
    return page(
        request,
        "scan_op.html",
        {
            "title": "Сканер приёмки",
            "hint": "Скан этикетки готового зеркала — зеркало встаёт на склад.",
            "url": "/scan/accept",
            "button": "Принять",
        },
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
