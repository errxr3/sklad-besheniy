"""Журнал движений и отчёт «Выработка» (SPEC §6.10)."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Article, Site, User
from app.services.journal import JournalFilters, journal_csv, journal_page, output_report
from app.statuses import ALL_MOVEMENT_TYPES, MOVEMENT_LABELS
from app.timeutil import today_msk
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


def _filters(
    date_from: str, date_to: str, article_id: int, user_id: int, movement_type: str, site_id: int,
    number: str,
) -> JournalFilters:
    """Собрать фильтры журнала из параметров адреса."""
    return JournalFilters(
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        article_id=article_id or None,
        user_id=user_id or None,
        movement_type=movement_type or None,
        site_id=site_id or None,
        number=number or None,
    )


@router.get("/journal", response_class=HTMLResponse)
def journal(
    request: Request,
    db: Session = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
    article_id: int = 0,
    user_id: int = 0,
    movement_type: str = "",
    site_id: int = 0,
    number: str = "",
    page_number: int = 1,
) -> Response:
    """Журнал движений с фильтрами и пагинацией."""
    filters = _filters(date_from, date_to, article_id, user_id, movement_type, site_id, number)
    rows, total = journal_page(db, filters, page=max(1, page_number))
    return page(
        request,
        "journal.html",
        {
            "rows": rows,
            "total": total,
            "page_number": max(1, page_number),
            "pages": max(1, (total + 49) // 50),
            "users": db.scalars(select(User).order_by(User.name)).all(),
            "sites": db.scalars(select(Site).order_by(Site.sort_order)).all(),
            "articles": db.scalars(
                select(Article).where(Article.kind == "article").order_by(Article.sort_order).limit(500)
            ).all(),
            "types": [(t, MOVEMENT_LABELS.get(t, t)) for t in ALL_MOVEMENT_TYPES],
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
                "article_id": article_id,
                "user_id": user_id,
                "movement_type": movement_type,
                "site_id": site_id,
                "number": number,
            },
        },
    )


@router.get("/journal.csv")
def journal_export(
    request: Request,
    db: Session = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
    article_id: int = 0,
    user_id: int = 0,
    movement_type: str = "",
    site_id: int = 0,
    number: str = "",
) -> Response:
    """Журнал в CSV."""
    filters = _filters(date_from, date_to, article_id, user_id, movement_type, site_id, number)
    data = journal_csv(db, filters)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="journal.csv"'},
    )


@router.get("/reports/output", response_class=HTMLResponse)
def output(
    request: Request,
    db: Session = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
    site_id: int = 0,
) -> Response:
    """Отчёт «Выработка»: сделано, брак, процент брака."""
    today = today_msk()
    start = date.fromisoformat(date_from) if date_from else today - timedelta(days=7)
    end = date.fromisoformat(date_to) if date_to else today
    rows = output_report(db, date_from=start, date_to=end, site_id=site_id or None)
    return page(
        request,
        "output.html",
        {
            "rows": rows,
            "date_from": start,
            "date_to": end,
            "site_id": site_id,
            "sites": db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all(),
            "total_made": sum(row.made for row in rows),
            "total_scrapped": sum(row.scrapped for row in rows),
        },
    )
