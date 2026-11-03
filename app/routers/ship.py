"""Партии BLAGO OTGRUZKI и вопросы начальнику по нехватке (решения владельца 2026-10-29)."""

import logging
from datetime import date, datetime, time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Article, Site, User
from app.services.admin import log_admin
from app.services.batches import (
    BatchError,
    answer_all,
    answer_question,
    day_batches,
    open_questions,
    remove_items,
    return_items,
)
from app.services.legacy_stock import add_found_legacy
from app.services.ship_plan import STATE_LABELS, build_ship_plan, ship_plan_xlsx, visible_rows
from app.services.shipping import ShippingError, manual_channels, plan_sites_channels, set_manual
from app.services.tasks import TaskError, site_workers, task_sites, tasks_for_article, urgent_tasks
from app.services.units import UnitError
from app.statuses import PLAN_CHANNELS, REMOVE_REASONS, ROLE_SHIFT_LEAD, SHIP_STATE_LABELS
from app.timeutil import MOSCOW, fmt_time, now_utc, today_msk
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


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw) if raw else today_msk()
    except ValueError:
        return today_msk()


@router.get("/shipments/plan", response_class=HTMLResponse)
def plan_page(request: Request, db: Session = Depends(get_db), day: str = "", full: str = "") -> Response:
    """План отгрузки: заказы дня из BLAGO OTGRUZKI против остатка, задач и погруженного."""
    plan = build_ship_plan(db, day=_day(day))
    return page(
        request,
        "ship_plan.html",
        {
            "day": plan.day,
            "plan": plan,
            "rows": visible_rows(plan, show_all=bool(full)),
            "show_all": bool(full),
            "state_labels": STATE_LABELS,
        },
    )


@router.get("/shipments/plan.xlsx")
def plan_xlsx(request: Request, db: Session = Depends(get_db), day: str = "") -> Response:
    """План отгрузки в Excel."""
    from app.routers.stock import _attachment

    plan = build_ship_plan(db, day=_day(day))
    name = f"План_отгрузки_{plan.day:%d.%m.%Y}_{now_utc().strftime('%H-%M')}.xlsx"
    return Response(
        content=ship_plan_xlsx(plan),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment(name, "plan.xlsx")},
    )


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
            "today": today_msk(),
            "channel_state": _channel_state(db),
            "found_sites": _found_sites(db, user),
            "article_names": db.scalars(
                select(Article.name).where(Article.kind == "article", Article.active.is_(True))
                .order_by(Article.sort_order)
            ).all(),
            "batches": day_batches(db, day=target, site_ids=lead_site_ids(db, user)),
            "reasons": REMOVE_REASONS,
            "state_labels": SHIP_STATE_LABELS,
            "ok": ok,
            "err": err,
        },
    )


def _found_sites(db: Session, user: User) -> list[Site]:
    """Площадки для «Нашлось старое»: начальник с площадкой — своя, иначе все (первой — своя)."""
    allowed = lead_site_ids(db, user)
    sites = db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.id)).all()
    sites = [site for site in sites if allowed is None or site.id in allowed]
    return sorted(sites, key=lambda site: site.id != user.home_site_id)


def _channel_state(db: Session) -> list[tuple[str, str, str]]:
    """Озон и ВБ сегодня: (канал, plan | partial | manual | closed, пояснение). Сразу для обеих площадок."""
    sites = db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.id)).all()
    planned = plan_sites_channels(db)
    manual = manual_channels(db)
    out: list[tuple[str, str, str]] = []
    for channel in PLAN_CHANNELS:
        with_plan = [site.name for site in sites if (site.id, channel) in planned]
        if channel in manual:
            row = manual[channel]
            who = row.user.name if row.user else ""
            out.append((channel, "manual", f"{who}, {fmt_time(row.created_at)}"))
        elif with_plan and len(with_plan) == len(sites):
            out.append((channel, "plan", ""))
        elif with_plan:
            out.append((channel, "partial", ", ".join(with_plan)))
        else:
            out.append((channel, "closed", ""))
    return out


@router.post("/shipments/manual")
def shipments_manual(
    request: Request,
    db: Session = Depends(get_db),
    channel: str = Form(...),
    on: str = Form(""),
) -> Response:
    """«Грузить без плана»: канал сегодня грузят без плана на обеих площадках (или закрыть обратно)."""
    user = request.state.user
    enable = on == "1"
    try:
        changed = set_manual(db, channel=channel, actor=user, on=enable)
    except ShippingError as exc:
        return _back("/shipments/batches", err=str(exc))
    if changed:
        log_admin(db, user.id, "manual_channel_on" if enable else "manual_channel_off",
                  after={"channel": channel, "day": today_msk().isoformat()})
    db.commit()
    title = "Озон" if channel == "Ozon" else channel
    if enable:
        ok = f"{title}: сегодня грузят без плана, только по нашим этикеткам"
    else:
        ok = f"{title}: погрузка без плана закрыта"
    logger.info("Канал без плана", extra={"user_id": user.id, "status": ok})
    return _back("/shipments/batches", ok=ok)


@router.post("/shipments/found-old")
def shipments_found_old(
    request: Request,
    db: Session = Depends(get_db),
    article: str = Form(""),
    site_id: int = Form(...),
    qty: int = Form(1),
) -> Response:
    """«Нашлось старое»: склад просит нашу этикетку, а старых по складу нет — добавить старое в остаток."""
    user = request.state.user
    allowed = lead_site_ids(db, user)
    if allowed is not None and site_id not in allowed:
        return _back("/shipments/batches", err="Это не ваша площадка")
    try:
        found = add_found_legacy(db, article_name=article.strip(), site_id=site_id, qty=qty, actor=user)
    except UnitError as exc:
        return _back("/shipments/batches", err=str(exc))
    log_admin(db, user.id, "found_old", "article", found.id, after={"site_id": site_id, "qty": qty})
    db.commit()
    logger.info("Нашлось старое", extra={"user_id": user.id, "status": f"{found.name} +{qty}"})
    return _back("/shipments/batches", ok=f"Добавлено старых без этикетки: {found.name} +{qty}. "
                                         "Пусть грузчик снова отсканирует наклейку маркетплейса")


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
