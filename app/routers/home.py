"""Главная страница: своя для каждой роли (SPEC §4)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Article, Site, Unit, User
from app.services.legacy_stock import legacy_counts
from app.services.shifts import open_shift, shift_sites
from app.services.task_labels import assembler_tasks, upcoming_tasks
from app.services.tasks import urgent_tasks
from app.statuses import IN_STOCK, ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_RETURNS, ROLE_STOREKEEPER
from app.timeutil import today_msk
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


def loader_stock(
    db: Session, user: User
) -> tuple[list[Site], list[tuple[str, list[tuple[int, int]]]]]:
    """Остатки для грузчика: артикулы, которые есть на складе, по площадкам (своя — первой)."""
    sites = sorted(
        db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id)).all(),
        key=lambda site: site.id != user.home_site_id,
    )
    counts: dict[tuple[int, int], int] = {
        (int(a), int(s)): int(n)
        for a, s, n in db.execute(
            select(Unit.article_id, Unit.site_id, func.count())
            .where(Unit.status == IN_STOCK)
            .group_by(Unit.article_id, Unit.site_id)
        ).all()
    }
    present = {article_id for article_id, _ in counts}
    articles = db.scalars(
        select(Article).where(Article.id.in_(present)).order_by(Article.sort_order)
    ).all() if present else []
    old = legacy_counts(db)
    rows = [
        (a.name, [(counts.get((a.id, s.id), 0), old.get((a.id, s.id), 0)) for s in sites]) for a in articles
    ]
    return sites, rows


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
                "upcoming": upcoming_tasks(db, user_id=user.id, day=today_msk())
                if user.role == ROLE_ASSEMBLER
                else [],
                "urgent": urgent_tasks(db, day=today_msk(), user_id=user.id)
                if user.role == ROLE_ASSEMBLER
                else [],
                # Грузчику — остатки склада, только посмотреть
                "stock": loader_stock(db, user) if user.role == ROLE_LOADER else None,
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


# Метка «что-то поменялось»: новый скан, отмена, план, канал, смена. Дёшево — по первичным ключам.
_STAMP_SQL = text(
    "SELECT concat_ws('-',"
    " (SELECT max(id) FROM movements),"
    " (SELECT max(id) FROM ship_items),"
    " (SELECT max(id) FROM pallets), (SELECT count(*) FROM pallets WHERE closed_at IS NULL),"
    " (SELECT max(id) FROM shifts), (SELECT count(*) FROM shifts WHERE ended_at IS NULL),"
    " (SELECT max(id) FROM manual_channels), (SELECT count(*) FROM manual_channels))"
)


@router.get("/live/stamp", response_class=PlainTextResponse)
def live_stamp(db: Session = Depends(get_db)) -> str:
    """Таблицы на экранах обновляются сразу после скана: страница сверяет эту метку раз в 3 с."""
    return str(db.scalar(_STAMP_SQL) or "")
