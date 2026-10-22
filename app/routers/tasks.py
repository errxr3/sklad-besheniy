"""Задачи на день: постановка начальником смены и отметка выполнения (SPEC §6.1–6.2)."""

import json
import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Article, Site, Task, User
from app.services.task_import import TasksFileError, parse_tasks_file
from app.services.tasks import (
    TaskError,
    create_task,
    delete_task,
    done_count,
    mark_text_task_done,
    plan_fact_report,
    site_workers,
    task_sites,
    tasks_progress,
    update_task,
)
from app.timeutil import today_msk
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def hanging_count(db: Session, task_id: int) -> int:
    """Сколько этикеток задачи напечатано, а зеркала ещё не на складе."""
    from sqlalchemy import func

    from app.models import Unit
    from app.statuses import PRINTED

    value = db.scalar(
        select(func.count()).select_from(Unit).where(Unit.task_id == task_id, Unit.status == PRINTED)
    )
    return int(value or 0)


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
    user = request.state.user
    # Начальник смены с площадкой в карточке видит только её задачи
    sites = task_sites(db, user)
    by_id = {s.id: s for s in sites}
    home = by_id.get(user.home_site_id) if user.home_site_id else None
    # По умолчанию — площадка начальника смены, иначе первая доступная
    site = by_id.get(site_id) or home or (sites[0] if sites else None)
    workers = site_workers(db, site.id) if site is not None else []

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
            by_user.setdefault(task.user_id, []).append(
                {"task": task, "done": done, "hanging": hanging_count(db, task.id)}
            )

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
        task = update_task(
            db,
            actor=request.state.user,
            task_id=task_id,
            qty_plan=int(qty_plan) if qty_plan else None,
            text=text,
        )
    except (TaskError, ValueError) as exc:
        return _back(target_day, site_id, err=str(exc))
    message = "Задача изменена"
    if task.kind == "article" and task.article_id and task.qty_plan:
        # План уменьшили, а этикетки уже напечатаны — лишние кладовщица уберёт сканом
        done = done_count(db, user_id=task.user_id, article_id=task.article_id, day=task.date)
        extra = hanging_count(db, task.id) - max(0, task.qty_plan - done)
        if extra > 0:
            message += (
                f". Лишних напечатанных этикеток: {extra} — сборщик отдаёт их кладовщице,"
                " она уберёт их в «Лишние этикетки»"
            )
    return _back(target_day, site_id, message=message)


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
    hanging = hanging_count(db, task_id)
    try:
        delete_task(db, actor=request.state.user, task_id=task_id)
    except TaskError as exc:
        return _back(target_day, site_id, err=str(exc))
    message = "Задача удалена"
    if hanging:
        # Этикетки удалённой задачи уже напечатаны: их убирает кладовщица сканом
        message += (
            f". Напечатанные этикетки ({hanging} шт.) сборщик отдаёт кладовщице —"
            " она уберёт их в «Лишние этикетки»"
        )
    return _back(target_day, site_id, message=message)


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


@router.post("/tasks/submit")
def tasks_submit(
    request: Request,
    task_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """«Собрал всё»: все висящие этикетки задачи сборщика — на склад без скана."""
    from app.services.context import site_and_shift
    from app.services.task_labels import submit_task
    from app.services.units import UnitError

    user = request.state.user
    _, shift_id = site_and_shift(db, user)
    try:
        count = submit_task(db, task_id=task_id, actor=user, shift_id=shift_id)
    except UnitError as exc:
        return RedirectResponse(f"/?{urlencode({'err': str(exc)})}", status_code=303)
    logger.info("Задача сдана кнопкой", extra={"user_id": user.id, "status": f"{count} шт."})
    return RedirectResponse(f"/?{urlencode({'ok': f'На склад записано: {count} шт.'})}", status_code=303)


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


# ---------- Задачи из Excel ----------


def _import_site(db: Session, user: User, site_id: int) -> Site | None:
    sites = task_sites(db, user)
    by_id = {s.id: s for s in sites}
    home = by_id.get(user.home_site_id) if user.home_site_id else None
    return by_id.get(site_id) or home or (sites[0] if sites else None)


@router.get("/tasks/import", response_class=HTMLResponse)
def tasks_import_form(
    request: Request, db: Session = Depends(get_db), site_id: int = 0, err: str = ""
) -> Response:
    """Форма загрузки задач из Excel."""
    user = request.state.user
    sites = task_sites(db, user)
    site = _import_site(db, user, site_id)
    return page(
        request,
        "tasks_import.html",
        {
            "sites": sites,
            "site": site,
            "day": today_msk() + timedelta(days=1),
            "err": err,
            "preview": None,
        },
    )


@router.post("/tasks/import/preview", response_class=HTMLResponse)
async def tasks_import_preview(
    request: Request,
    day: str = Form(...),
    site_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    """Разобрать файл: всё узнано — поставить задачи сразу, иначе показать, что не сошлось."""
    user = request.state.user
    site = _import_site(db, user, site_id)
    sites = task_sites(db, user)
    try:
        target_day = date.fromisoformat(day)
    except ValueError:
        return RedirectResponse("/tasks/import", status_code=303)
    content = await file.read()
    error, preview = "", None
    if site is None or site.id != site_id:
        error = "Эта площадка вам недоступна"
    elif target_day < today_msk():
        error = "Задачи за прошедшие дни можно только смотреть"
    elif not content:
        error = "Файл пустой"
    else:
        try:
            preview = parse_tasks_file(db, content, site)
        except TasksFileError as exc:
            error = str(exc)
    if site is not None and preview is not None and preview.clean:
        owner = {c.index: c.user_id for c in preview.columns}
        wanted: dict[tuple[int, int], int] = {}
        for cell in preview.cells:
            key = (owner[cell.col] or 0, cell.article_id)
            wanted[key] = wanted.get(key, 0) + cell.qty
        return _place_tasks(db, user, target_day, site.id, wanted, {})
    return page(
        request,
        "tasks_import.html",
        {"sites": sites, "site": site, "day": target_day, "err": error, "preview": preview},
    )


@router.post("/tasks/import/apply")
async def tasks_import_apply(request: Request, db: Session = Depends(get_db)) -> Response:
    """Поставить задачи из предпросмотра: колонки без выбранного сотрудника пропускаются."""
    form = await request.form()
    user = request.state.user
    try:
        target_day = date.fromisoformat(str(form.get("day", "")))
        site_id = int(str(form.get("site_id", "0")))
    except ValueError:
        return RedirectResponse("/tasks/import", status_code=303)
    try:
        cells = json.loads(str(form.get("payload", "[]")))
        wanted: dict[tuple[int, int], int] = {}
        skipped_cols: dict[int, int] = {}
        for item in cells:
            col, article_id, qty = int(item["col"]), int(item["article_id"]), int(item["qty"])
            chosen = str(form.get(f"col_{col}", ""))
            if not chosen.isdigit():
                skipped_cols[col] = skipped_cols.get(col, 0) + qty
                continue
            key = (int(chosen), article_id)
            wanted[key] = wanted.get(key, 0) + qty
    except (ValueError, KeyError, TypeError):
        return _back(target_day, site_id, err="Данные предпросмотра устарели, загрузите файл ещё раз")
    return _place_tasks(db, user, target_day, site_id, wanted, skipped_cols)


def _place_tasks(
    db: Session,
    user: User,
    target_day: date,
    site_id: int,
    wanted: dict[tuple[int, int], int],
    skipped_cols: dict[int, int],
) -> Response:
    """Поставить задачи (сотрудник, артикул) → штук и вернуться к задачам дня с итогом."""
    created, qty_total, problems = 0, 0, []
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_({k[0] for k in wanted}))).all()}
    for (worker_id, article_id), qty in sorted(wanted.items()):
        try:
            create_task(
                db,
                actor=user,
                day=target_day,
                site_id=site_id,
                user_id=worker_id,
                kind="article",
                article_id=article_id,
                qty_plan=qty,
            )
            created += 1
            qty_total += qty
        except TaskError as exc:
            article = db.get(Article, article_id)
            who = names.get(worker_id, str(worker_id))
            problems.append(f"{who}, {article.name if article else article_id}: {exc}")
    db.commit()
    message = f"Поставлено задач: {created} ({qty_total} шт.)"
    parts = []
    if skipped_cols:
        skipped_qty = sum(skipped_cols.values())
        parts.append(f"пропущено колонок без сотрудника: {len(skipped_cols)} ({skipped_qty} шт.)")
    if problems:
        parts.append("не поставлено: " + "; ".join(problems[:6]) + (" и другие" if len(problems) > 6 else ""))
    logger.info("Задачи загружены из Excel", extra={"user_id": user.id, "status": f"{created} задач"})
    return _back(target_day, site_id, message=message, err="; ".join(parts))
