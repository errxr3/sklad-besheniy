"""Задачи на день: постановка начальником смены и отметка выполнения (SPEC §6.1–6.2)."""

import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Site, Task, User
from app.services.tasks import (
    TaskError,
    copy_remaining,
    create_task,
    delete_task,
    done_count,
    mark_text_task_done,
    plan_fact_report,
    tasks_progress,
    update_task,
)
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER
from app.timeutil import today_msk
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _back(day: date, site_id: int, message: str = "", err: str = "") -> RedirectResponse:
    """Вернуться на экран задач с сообщением."""
    params: dict[str, str] = {"day": day.isoformat(), "site_id": str(site_id)}
    if message:
        params["ok"] = message
    if err:
        params["err"] = err
    return RedirectResponse(f"/tasks?{urlencode(params)}", status_code=303)


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request,
    db: Session = Depends(get_db),
    day: str = "",
    site_id: int = 0,
    ok: str = "",
    err: str = "",
) -> Response:
    """Экран задач: по умолчанию — завтра (SPEC §6.2)."""
    target_day = date.fromisoformat(day) if day else today_msk() + timedelta(days=1)
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())
    user = request.state.user
    home = next((s for s in sites if s.id == user.home_site_id), None)
    # По умолчанию — площадка начальника смены, иначе первая активная
    site = db.get(Site, site_id) if site_id else (home or (sites[0] if sites else None))
    workers = list(
        db.scalars(
            select(User)
            .where(User.active.is_(True), User.role.in_((ROLE_ASSEMBLER, ROLE_LOADER)))
            .order_by(User.role, User.name)
        ).all()
    )

    by_user: dict[int, list[dict[str, object]]] = {}
    if site is not None:
        tasks = db.scalars(
            select(Task).where(Task.date == target_day, Task.site_id == site.id).order_by(Task.id)
        ).all()
        for task in tasks:
            done = (
                done_count(db, user_id=task.user_id, article_id=task.article_id, day=target_day)
                if task.kind == "article" and task.article_id
                else (1 if task.done_manually_at else 0)
            )
            by_user.setdefault(task.user_id, []).append({"task": task, "done": done})

    return page(
        request,
        "tasks.html",
        {
            "day": target_day,
            "yesterday": target_day - timedelta(days=1),
            "sites": sites,
            "site": site,
            "workers": workers,
            "by_user": by_user,
            "editable": target_day >= today_msk(),
            "today": today_msk(),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/tasks/create")
def tasks_create(
    request: Request,
    day: str = Form(...),
    site_id: int = Form(...),
    user_id: int = Form(...),
    kind: str = Form(...),
    article_id: str = Form(""),
    qty_plan: str = Form(""),
    text: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Поставить задачу."""
    target_day = date.fromisoformat(day)
    try:
        create_task(
            db,
            actor=request.state.user,
            day=target_day,
            site_id=site_id,
            user_id=user_id,
            kind=kind,
            article_id=int(article_id) if article_id else None,
            qty_plan=int(qty_plan) if qty_plan else None,
            text=text,
        )
    except (TaskError, ValueError) as exc:
        return _back(target_day, site_id, err=str(exc))
    return _back(target_day, site_id, message="Задача поставлена")


@router.post("/tasks/update")
def tasks_update(
    request: Request,
    task_id: int = Form(...),
    day: str = Form(...),
    site_id: int = Form(...),
    qty_plan: str = Form(""),
    text: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Изменить задачу."""
    target_day = date.fromisoformat(day)
    try:
        update_task(
            db,
            actor=request.state.user,
            task_id=task_id,
            qty_plan=int(qty_plan) if qty_plan else None,
            text=text,
        )
    except (TaskError, ValueError) as exc:
        return _back(target_day, site_id, err=str(exc))
    return _back(target_day, site_id, message="Задача изменена")


@router.post("/tasks/delete")
def tasks_delete(
    request: Request,
    task_id: int = Form(...),
    day: str = Form(...),
    site_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Удалить задачу."""
    target_day = date.fromisoformat(day)
    try:
        delete_task(db, actor=request.state.user, task_id=task_id)
    except TaskError as exc:
        return _back(target_day, site_id, err=str(exc))
    return _back(target_day, site_id, message="Задача удалена")


@router.post("/tasks/copy")
def tasks_copy(
    request: Request,
    day: str = Form(...),
    site_id: int = Form(...),
    source_day: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Скопировать невыполненный остаток плана на выбранную дату."""
    target_day = date.fromisoformat(day)
    try:
        count = copy_remaining(
            db,
            actor=request.state.user,
            source_day=date.fromisoformat(source_day),
            target_day=target_day,
            site_id=site_id,
        )
    except TaskError as exc:
        return _back(target_day, site_id, err=str(exc))
    return _back(target_day, site_id, message=f"Перенесено задач: {count}")


@router.post("/tasks/done")
def tasks_done(
    request: Request,
    task_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Отметить текстовую задачу выполненной (кнопка «Готово» у сотрудника)."""
    try:
        mark_text_task_done(db, actor=request.state.user, task_id=task_id)
    except TaskError as exc:
        return RedirectResponse(f"/?{urlencode({'err': str(exc)})}", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.get("/reports/plan-fact", response_class=HTMLResponse)
def plan_fact(
    request: Request,
    db: Session = Depends(get_db),
    date_from: str = "",
    date_to: str = "",
    site_id: int = 0,
) -> Response:
    """Отчёт «Задачи план/факт» за период."""
    today = today_msk()
    start = date.fromisoformat(date_from) if date_from else today - timedelta(days=7)
    end = date.fromisoformat(date_to) if date_to else today
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.sort_order)).all())
    rows = plan_fact_report(db, date_from=start, date_to=end, site_id=site_id or None)
    return page(
        request,
        "plan_fact.html",
        {
            "rows": rows,
            "date_from": start,
            "date_to": end,
            "sites": sites,
            "site_id": site_id,
            "total_plan": sum(row.plan for row in rows),
            "total_done": sum(row.done for row in rows),
        },
    )


@router.get("/reports", response_class=HTMLResponse)
def reports_home(request: Request) -> Response:
    """Меню отчётов."""
    return page(request, "reports.html", {})


def progress_for(db: Session, user: User) -> object:
    """Прогресс сотрудника на сегодня (используется главной страницей)."""
    return tasks_progress(db, user_id=user.id, day=today_msk())
