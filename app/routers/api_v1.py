"""API для программы отгрузок BLAGO OTGRUZKI (SPEC §10).

Доступ по ключу в заголовке `X-API-Key`. Ключи создаёт администратор.
"""

import logging
from datetime import date as DateType

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ShipQuestion
from app.services.batches import BatchError, receive_batch, receive_demand
from app.timeutil import fmt_time, today_msk
from app.web import GuardedRoute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", route_class=GuardedRoute)


@router.get("/ping")
def api_ping(request: Request) -> JSONResponse:
    """Проверка ключа."""
    return JSONResponse({"ok": True})


# ---------- BLAGO OTGRUZKI: заказы дня и партии этикеток ----------


def _day(value: object) -> DateType:
    try:
        return DateType.fromisoformat(str(value)) if value else today_msk()
    except ValueError:
        return today_msk()


@router.post("/demand")
async def api_demand(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Заказы дня → что едет. Ответ по каждому отправлению: go / ask / no и почему.

    Тело: {"day": "2026-10-29", "replace": ["ozon", "wb"], "lines": [{market, channel, truck,
    posting, article, qty, production, ordered_at}]}. `replace` — полный утренний список
    этих маркетплейсов: заказы, которых в нём нет и на которые нет этикеток, убираются.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        return JSONResponse({"ok": False, "error": "Ожидается {day, lines: [...]}"}, status_code=400)
    replace = [str(m) for m in payload.get("replace") or [] if isinstance(m, str)]
    try:
        lines = receive_demand(
            db, day=_day(payload.get("day")), lines=[x for x in payload["lines"] if isinstance(x, dict)],
            replace_markets=replace or None,
        )
    except BatchError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    questions = db.scalars(select(ShipQuestion).where(ShipQuestion.day == _day(payload.get("day")))).all()
    logger.info("Приняты заказы дня", extra={"status": f"{len(lines)} строк"})
    return JSONResponse(
        {
            "ok": True,
            "postings": {
                line.posting: {"decision": line.decision, "reason": line.reason or ""} for line in lines
            },
            "questions": [
                {
                    "article": q.article.name, "site": q.site.name, "need": q.need, "stock": q.stock,
                    "in_work": q.in_work, "short": q.short, "status": q.answer, "promised": q.promised,
                    "due": fmt_time(q.due_at) if q.due_at else "",
                }
                for q in questions
                if q.short > 0
            ],
        }
    )


@router.post("/batches")
async def api_batches(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Партия этикеток: {day, market, channel, truck, source, items: [{posting, article,
    production, codes, ordered_at}]}. Повторная отправка той же волны дополняет партию."""
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        error = "Ожидается {market, channel, items: [...]}"
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    try:
        result = receive_batch(
            db,
            day=_day(payload.get("day")),
            market=str(payload.get("market") or ""),
            channel=str(payload.get("channel") or ""),
            truck=str(payload.get("truck") or ""),
            source=str(payload.get("source") or ""),
            items=[x for x in payload["items"] if isinstance(x, dict)],
        )
    except BatchError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    logger.info(
        "Принята партия",
        extra={"status": f"{result.batch.title}: +{result.added}, обновлено {result.updated}"},
    )
    return JSONResponse(
        {
            "ok": True,
            "batch": result.batch.title,
            "added": result.added,
            "updated": result.updated,
            "kept": result.kept,
            "unknown": result.unknown,
        }
    )
