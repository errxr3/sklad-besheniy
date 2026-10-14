"""Задачи на день: постановка, прогресс, копирование, отчёт (SPEC §6.1–6.2)."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Movement, Task, TaskLog, Unit, User
from app.statuses import (
    IN_STOCK,
    MV_ACCEPT,
    MV_INVENTORY_FIX,
    MV_LABEL_VOID,
    MV_WRITE_OFF,
    PRINTED,
    ROLE_ASSEMBLER,
    WRITTEN_OFF,
)
from app.timeutil import msk_day_bounds, now_utc, today_msk


class TaskError(Exception):
    """Ошибка работы с задачей, текст показывается пользователю."""


@dataclass
class TaskView:
    """Задача с посчитанным фактом."""

    task: Task
    done: int

    @property
    def plan(self) -> int:
        """План по задаче (для текстовой задачи — 1)."""
        return self.task.qty_plan or 1

    @property
    def is_done(self) -> bool:
        """Задача выполнена."""
        if self.task.kind == "text":
            return self.task.done_manually_at is not None
        return self.done >= self.plan

    @property
    def percent(self) -> int:
        """Процент выполнения для полосы прогресса."""
        if self.task.kind == "text":
            return 100 if self.is_done else 0
        return min(100, int(self.done * 100 / self.plan)) if self.plan else 0


@dataclass
class Progress:
    """Суммарный прогресс сотрудника за день."""

    plan: int = 0
    done: int = 0
    views: list[TaskView] = field(default_factory=list)


def done_count(session: Session, *, user_id: int, article_id: int, day: date) -> int:
    """Сколько зеркал этого артикула сборщик сдал за день (SPEC §6.1).

    Считаются зеркала, где сборщик — этот сотрудник и переход «Напечатано» → «На складе»
    произошёл в этот день по Москве, за вычетом списанных в брак в тот же день.
    """
    start, end = msk_day_bounds(day)

    accepted = select(Movement.unit_id).join(Unit, Unit.id == Movement.unit_id).where(
        Movement.type == MV_ACCEPT,
        Movement.from_status == PRINTED,
        Movement.created_at >= start,
        Movement.created_at < end,
        Unit.assembler_id == user_id,
        Unit.article_id == article_id,
    )
    accepted_ids = set(session.scalars(accepted).all())
    if not accepted_ids:
        return 0

    scrapped = select(Movement.unit_id).where(
        Movement.unit_id.in_(accepted_ids),
        Movement.to_status == WRITTEN_OFF,
        Movement.type.in_((MV_WRITE_OFF, MV_LABEL_VOID, MV_INVENTORY_FIX)),
        Movement.created_at >= start,
        Movement.created_at < end,
    )
    scrapped_ids = set(session.scalars(scrapped).all())
    return len(accepted_ids - scrapped_ids)


def tasks_for(session: Session, *, user_id: int, day: date) -> list[Task]:
    """Задачи сотрудника на дату."""
    return list(
        session.scalars(
            select(Task).where(Task.user_id == user_id, Task.date == day).order_by(Task.id)
        ).all()
    )


def tasks_progress(session: Session, *, user_id: int, day: date) -> Progress:
    """Задачи сотрудника с посчитанным фактом."""
    progress = Progress()
    for task in tasks_for(session, user_id=user_id, day=day):
        if task.kind == "article" and task.article_id:
            done = done_count(session, user_id=user_id, article_id=task.article_id, day=day)
        else:
            done = 1 if task.done_manually_at else 0
        view = TaskView(task=task, done=done)
        progress.views.append(view)
        progress.plan += view.plan
        progress.done += min(view.done, view.plan)
    return progress


def _snapshot(task: Task) -> dict[str, Any]:
    """Состояние задачи для журнала задач."""
    return {
        "date": task.date.isoformat(),
        "user_id": task.user_id,
        "site_id": task.site_id,
        "kind": task.kind,
        "article_id": task.article_id,
        "qty_plan": task.qty_plan,
        "text": task.text,
    }


def log_task(
    session: Session,
    *,
    task_id: int | None,
    user_id: int,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Запись в журнал изменений задач."""
    session.add(
        TaskLog(task_id=task_id, user_id=user_id, action=action, before=before, after=after)
    )


def _check_editable(day: date) -> None:
    """Прошедшие дни только просматриваются (SPEC §6.2)."""
    if day < today_msk():
        raise TaskError("Задачи за прошедшие дни можно только смотреть")


def create_task(
    session: Session,
    *,
    actor: User,
    day: date,
    site_id: int,
    user_id: int,
    kind: str,
    article_id: int | None = None,
    qty_plan: int | None = None,
    text: str | None = None,
) -> Task:
    """Поставить задачу сотруднику."""
    _check_editable(day)
    worker = session.get(User, user_id)
    if worker is None or not worker.active:
        raise TaskError("Сотрудник не найден")

    if kind == "article":
        if worker.role != ROLE_ASSEMBLER:
            raise TaskError("Задача с артикулом ставится только сборщику")
        article = session.get(Article, article_id) if article_id else None
        if article is None or article.kind != "article":
            raise TaskError("Артикул не найден")
        if not qty_plan or qty_plan < 1:
            raise TaskError("Укажите количество")
        existing = session.scalar(
            select(Task).where(
                Task.date == day,
                Task.user_id == user_id,
                Task.article_id == article.id,
                Task.kind == "article",
            )
        )
        if existing is not None:
            raise TaskError(f"Задача по «{article.name}» на этот день уже есть — измените её")
        task = Task(
            date=day,
            site_id=site_id,
            user_id=user_id,
            kind="article",
            article_id=article.id,
            qty_plan=qty_plan,
            created_by=actor.id,
        )
    elif kind == "text":
        if not text or not text.strip():
            raise TaskError("Напишите поручение")
        task = Task(
            date=day,
            site_id=site_id,
            user_id=user_id,
            kind="text",
            text=text.strip(),
            created_by=actor.id,
        )
    else:
        raise TaskError("Неизвестный вид задачи")

    session.add(task)
    session.flush()
    log_task(session, task_id=task.id, user_id=actor.id, action="создана", after=_snapshot(task))
    return task


def update_task(
    session: Session, *, actor: User, task_id: int, qty_plan: int | None = None, text: str | None = None
) -> Task:
    """Изменить задачу."""
    task = session.get(Task, task_id)
    if task is None:
        raise TaskError("Задача не найдена")
    _check_editable(task.date)
    before = _snapshot(task)
    if task.kind == "article":
        if qty_plan is None or qty_plan < 1:
            raise TaskError("Укажите количество")
        task.qty_plan = qty_plan
    else:
        if not text or not text.strip():
            raise TaskError("Напишите поручение")
        task.text = text.strip()
    session.flush()
    log_task(
        session,
        task_id=task.id,
        user_id=actor.id,
        action="изменена",
        before=before,
        after=_snapshot(task),
    )
    return task


def delete_task(session: Session, *, actor: User, task_id: int) -> None:
    """Удалить задачу (запись об этом остаётся в журнале задач)."""
    task = session.get(Task, task_id)
    if task is None:
        raise TaskError("Задача не найдена")
    _check_editable(task.date)
    before = _snapshot(task)
    log_task(session, task_id=task.id, user_id=actor.id, action="удалена", before=before)
    session.delete(task)
    session.flush()


def mark_text_task_done(session: Session, *, actor: User, task_id: int) -> Task:
    """Отметить текстовую задачу выполненной."""
    task = session.get(Task, task_id)
    if task is None:
        raise TaskError("Задача не найдена")
    if task.kind != "text":
        raise TaskError("Задача с артикулом считается автоматически")
    if task.user_id != actor.id and actor.role == ROLE_ASSEMBLER:
        raise TaskError("Это задача другого сотрудника")
    task.done_manually_at = now_utc()
    session.flush()
    log_task(session, task_id=task.id, user_id=actor.id, action="выполнена", after=_snapshot(task))
    return task


def copy_remaining(
    session: Session, *, actor: User, source_day: date, target_day: date, site_id: int
) -> int:
    """Перенести невыполненный остаток плана на другую дату (SPEC §6.2)."""
    _check_editable(target_day)
    tasks = session.scalars(
        select(Task).where(Task.date == source_day, Task.site_id == site_id, Task.kind == "article")
    ).all()
    created = 0
    for task in tasks:
        if not task.article_id or not task.qty_plan:
            continue
        done = done_count(
            session, user_id=task.user_id, article_id=task.article_id, day=source_day
        )
        remaining = task.qty_plan - done
        if remaining <= 0:
            continue
        existing = session.scalar(
            select(Task).where(
                Task.date == target_day,
                Task.user_id == task.user_id,
                Task.article_id == task.article_id,
                Task.kind == "article",
            )
        )
        if existing is not None:
            before = _snapshot(existing)
            existing.qty_plan = (existing.qty_plan or 0) + remaining
            session.flush()
            log_task(
                session,
                task_id=existing.id,
                user_id=actor.id,
                action="перенос остатка",
                before=before,
                after=_snapshot(existing),
            )
        else:
            new_task = Task(
                date=target_day,
                site_id=task.site_id,
                user_id=task.user_id,
                kind="article",
                article_id=task.article_id,
                qty_plan=remaining,
                created_by=actor.id,
            )
            session.add(new_task)
            session.flush()
            log_task(
                session,
                task_id=new_task.id,
                user_id=actor.id,
                action="перенос остатка",
                after=_snapshot(new_task),
            )
        created += 1
    return created


def plan_sum_by_site(session: Session, *, day: date) -> dict[tuple[int, int], int]:
    """Сумма плана задач по (артикул, площадка) на дату — для экрана «Остатки» (SPEC §6.9)."""
    rows = session.execute(
        select(Task.article_id, Task.site_id, func.sum(Task.qty_plan))
        .where(Task.date == day, Task.kind == "article")
        .group_by(Task.article_id, Task.site_id)
    ).all()
    return {
        (int(article_id), int(site_id)): int(total or 0)
        for article_id, site_id, total in rows
        if article_id is not None
    }


@dataclass
class PlanFactRow:
    """Строка отчёта «план/факт»."""

    user_name: str
    article_name: str
    plan: int
    done: int

    @property
    def percent(self) -> int:
        """Процент выполнения."""
        return int(self.done * 100 / self.plan) if self.plan else 0


def plan_fact_report(
    session: Session, *, date_from: date, date_to: date, site_id: int | None = None
) -> list[PlanFactRow]:
    """Отчёт по задачам за период: план и факт по сотрудникам и артикулам (SPEC §6.2)."""
    query = select(Task).where(
        Task.date >= date_from, Task.date <= date_to, Task.kind == "article"
    )
    if site_id:
        query = query.where(Task.site_id == site_id)
    tasks = session.scalars(query.order_by(Task.date)).all()

    totals: dict[tuple[str, str], list[int]] = {}
    for task in tasks:
        if not task.article_id:
            continue
        done = done_count(session, user_id=task.user_id, article_id=task.article_id, day=task.date)
        key = (task.user.name, task.article.name if task.article else "—")
        bucket = totals.setdefault(key, [0, 0])
        bucket[0] += task.qty_plan or 0
        bucket[1] += done
    return [
        PlanFactRow(user_name=user, article_name=article, plan=plan, done=done)
        for (user, article), (plan, done) in sorted(totals.items())
    ]


def yesterday_and_tomorrow(day: date | None = None) -> tuple[date, date]:
    """Вчера и завтра относительно московской даты."""
    base = day or today_msk()
    return base - timedelta(days=1), base + timedelta(days=1)


def stock_now(session: Session, *, article_id: int, site_id: int) -> int:
    """Текущий остаток артикула на площадке."""
    value = session.scalar(
        select(func.count())
        .select_from(Unit)
        .where(Unit.status == IN_STOCK, Unit.article_id == article_id, Unit.site_id == site_id)
    )
    return int(value or 0)
