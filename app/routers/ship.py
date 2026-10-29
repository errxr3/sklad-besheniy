"""Партии BLAGO OTGRUZKI и вопросы начальнику по нехватке (решения владельца 2026-10-29)."""

import logging
from datetime import date, datetime, time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services.batches import (
    BatchError,
    answer_all,
    answer_question,
    day_batches,
    open_questions,
    remove_items,
    return_items,
)
from app.services.tasks import TaskError, site_workers, task_sites, tasks_for_article, urgent_tasks
from app.statuses import REMOVE_REASONS, ROLE_SHIFT_LEAD, SHIP_STATE_LABELS
from app.timeutil import MOSCOW, today_msk
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def lead_site_ids(db: Session, user: User) -> list[int] | None:
    """Начальник с площадкой видит свою площадку, администратор и начальник без площадки — все."""
    if user.role == ROLE_SHIFT_LEAD and user.home_site_id:
        return [site.id for site in task_sites(db, user)]
    return None


def _back(url: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(url + (f"?{query}" if query else ""), status_code=303)


@router.get("/shipments/batches", response_class=HTMLResponse)
def batches_page(
    request: Request, db: Session = Depends(get_db), day: str = "", ok: str = "", err: str = ""
) -> Response:
    """Партии дня: что погружено, что осталось, что снято и что уехало другой машиной."""
    try:
        target = date.fromisoformat(day) if day else today_msk()
    except ValueError:
        target = today_msk()
    user = request.state.user
    return page(
        request,
        "ship_batches.html",
        {
            "day": target,
            "batches": day_batches(db, day=target, site_ids=lead_site_ids(db, user)),
            "reasons": REMOVE_REASONS,
            "state_labels": SHIP_STATE_LABELS,
            "ok": ok,
            "err": err,
        },
    )


async def _ids(request: Request) -> tuple[list[int], str, str]:
    form = await request.form()
    ids = [int(str(v)) for v in form.getlist("item_id") if str(v).isdigit()]
    reason = str(form.get("reason") or "")
    other = str(form.get("reason_text") or "").strip()
    return ids, (other if reason == "другое" and other else reason), str(form.get("day") or "")


@router.post("/shipments/batches/remove")
async def batches_remove(request: Request, db: Session = Depends(get_db)) -> Response:
    """Снять с отгрузки: зеркала нет — брак, не сделали, потеряли."""
    ids, reason, day = await _ids(request)
    user = request.state.user
    try:
        count = remove_items(db, actor=user, item_ids=ids, reason=reason, site_ids=lead_site_ids(db, user))
    except BatchError as exc:
        return _back("/shipments/batches", day=day, err=str(exc))
    logger.info("Снято с отгрузки", extra={"user_id": user.id, "status": f"{count} шт., {reason}"})
    return _back("/shipments/batches", day=day, ok=f"Снято с отгрузки: {count}" if count else "",
                 err="" if count else "Нечего снимать: выберите неотгруженные этикетки")


@router.post("/shipments/batches/return")
async def batches_return(request: Request, db: Session = Depends(get_db)) -> Response:
    """Вернуть снятое в отгрузку."""
    ids, _, day = await _ids(request)
    user = request.state.user
    count = return_items(db, item_ids=ids, site_ids=lead_site_ids(db, user))
    return _back("/shipments/batches", day=day, ok=f"Возвращено в отгрузку: {count}")


@router.get("/ship/questions", response_class=HTMLResponse)
def questions_page(request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "") -> Response:
    """Вопросы по нехватке и срочные задачи дня."""
    user = request.state.user
    site_ids = lead_site_ids(db, user)
    questions = open_questions(db, site_ids=site_ids)
    workers = {q.site_id: site_workers(db, q.site_id) for q in questions}
    # Кому подсказать: у кого этот артикул уже в работе
    busy = {
        q.id: tasks_for_article(db, day=q.day, site_id=q.site_id, article_id=q.article_id) for q in questions
    }
    return page(
        request,
        "ship_questions.html",
        {
            "questions": questions,
            "workers": workers,
            "busy": busy,
            "urgent": urgent_tasks(db, day=today_msk(), site_ids=site_ids),
            "ok": ok,
            "err": err,
        },
    )


def _due_today(raw: object) -> datetime | None:
    """«14:00» → сегодня 14:00 по Москве."""
    try:
        hours, minutes = (int(x) for x in str(raw or "").split(":")[:2])
        return datetime.combine(today_msk(), time(hours, minutes), tzinfo=MOSCOW)
    except ValueError:
        return None


@router.post("/ship/questions/answer-all")
async def questions_answer_all(request: Request, db: Session = Depends(get_db)) -> Response:
    """«Сделаем» на все вопросы: срочное тем, кто уже делает эти артикулы."""
    form = await request.form()
    user = request.state.user
    due_at = _due_today(form.get("due"))
    if due_at is None:
        return _back("/ship/questions", err="Укажите время, например 14:00")
    try:
        answered, skipped = answer_all(db, actor=user, due_at=due_at, site_ids=lead_site_ids(db, user))
    except (BatchError, TaskError) as exc:
        return _back("/ship/questions", err=str(exc))
    logger.info("Ответ на все вопросы", extra={"user_id": user.id, "status": f"{answered} / {skipped}"})
    ok = f"Срочное поставлено по {answered} артикулам"
    err = ""
    if skipped:
        err = f"Осталось {skipped}: эти артикулы сегодня никто не делает — выберите сборщика сами"
    return _back("/ship/questions", ok=ok, err=err)


@router.post("/ship/questions/{question_id}/answer")
async def question_answer(request: Request, question_id: int, db: Session = Depends(get_db)) -> Response:
    """«Сделаем к 14:00» (срочная задача сборщику) или «Нет, не успеем»."""
    form = await request.form()
    user = request.state.user
    yes = str(form.get("answer")) == "yes"
    due_at = _due_today(form.get("due")) if yes else None
    if yes and due_at is None:
        return _back("/ship/questions", err="Укажите время, например 14:00")
    try:
        assembler = int(str(form.get("assembler_id") or 0))
        qty = int(str(form.get("qty") or 0))
    except ValueError:
        return _back("/ship/questions", err="Проверьте сборщика и количество")
    try:
        question = answer_question(
            db, question_id=question_id, actor=user, yes=yes, assembler_id=assembler, qty=qty, due_at=due_at
        )
    except (BatchError, TaskError) as exc:
        return _back("/ship/questions", err=str(exc))
    name = question.article.name
    message = f"{name}: срочная задача поставлена" if yes else f"{name}: не едет, BLAGO OTGRUZKI узнает"
    logger.info("Ответ на вопрос по нехватке", extra={"user_id": user.id, "status": message})
    return _back("/ship/questions", ok=message)
