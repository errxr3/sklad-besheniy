"""API для программы отгрузок BLAGO OTGRUZKI (SPEC §10).

Доступ по ключу в заголовке `X-API-Key`. Ключи создаёт администратор.
"""

import logging
from datetime import date as DateType

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.articles import normalize_article
from app.db import get_db
from app.models import Article, MpShipment, Site, Unit
from app.statuses import IN_STOCK
from app.web import GuardedRoute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", route_class=GuardedRoute)


class ShipmentIn(BaseModel):
    """Отправление из программы отгрузок."""

    mp_barcode: str = Field(min_length=1, max_length=64)
    article: str = Field(min_length=1, max_length=200)
    date: DateType | None = None


@router.get("/stock")
def api_stock(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Остатки: [{article, site, qty}]."""
    from sqlalchemy import func

    rows = db.execute(
        select(Article.name, Site.name, func.count())
        .join(Unit, Unit.article_id == Article.id)
        .join(Site, Site.id == Unit.site_id)
        .where(Unit.status == IN_STOCK)
        .group_by(Article.name, Site.name)
        .order_by(Article.name, Site.name)
    ).all()
    return JSONResponse(
        [{"article": article, "site": site, "qty": int(qty)} for article, site, qty in rows]
    )


@router.post("/shipments")
async def api_shipments(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Приём списка отправлений дня. Идемпотентно: upsert по `mp_barcode`."""
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, list):
        return JSONResponse(
            {"ok": False, "error": "Ожидается список отправлений"}, status_code=400
        )

    added = 0
    updated = 0
    errors: list[str] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            errors.append(f"элемент {index}: ожидается объект")
            continue
        try:
            item = ShipmentIn.model_validate(raw)
        except Exception as exc:
            errors.append(f"элемент {index}: {exc.__class__.__name__}")
            continue

        barcode = item.mp_barcode.strip()
        existing = db.scalar(select(MpShipment).where(MpShipment.mp_barcode == barcode))
        if existing is None:
            db.add(
                MpShipment(
                    mp_barcode=barcode,
                    article_name=item.article.strip(),
                    article_name_norm=normalize_article(item.article),
                    date=item.date,
                )
            )
            added += 1
        else:
            existing.article_name = item.article.strip()
            existing.article_name_norm = normalize_article(item.article)
            existing.date = item.date
            updated += 1
    db.flush()
    logger.info("Приняты отправления", extra={"status": f"добавлено {added}, обновлено {updated}"})
    return JSONResponse({"ok": True, "added": added, "updated": updated, "errors": errors})


@router.get("/ping")
def api_ping(request: Request) -> JSONResponse:
    """Проверка ключа."""
    return JSONResponse({"ok": True})
