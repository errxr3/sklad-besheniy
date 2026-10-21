"""Печать этикеток по задачам сборщиков и уборка неиспользованных этикеток.

Кладовщик заранее печатает этикетки на план каждой задачи: этикетка сразу
подписана сборщиком и помнит свою задачу (`units.task_id`). Сборщик клеит её на
готовое зеркало и сканирует — зеркало встаёт на склад. Если к концу дня что-то не
сделано, оставшиеся «Напечатано» этикетки аннулируются (переход «Напечатано» →
«Списано» движением «Списание этикетки»), и их скан больше не пройдёт.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, func, not_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import Task, Unit, User
from app.services.tasks import done_count
from app.services.units import MAX_PRINT_QTY, UnitError, create_printed_units
from app.statuses import IN_STOCK, MV_ACCEPT, MV_LABEL_VOID, PRINTED, ROLE_ADMIN, WRITTEN_OFF
from app.transitions import apply_transition, lock_unit_by_id

VOID_NOTE = "этикетка не использована — зеркало не сделано"


@dataclass
class TaskLabels:
    """Задача сборщика с учётом напечатанных под неё этикеток."""

    task: Task
    issued: int  # напечатано и не аннулировано
    hanging: int  # напечатано, но ещё не принято сканом
    done: int  # сдано сборщиком за день (как в «Мой день»)

    @property
    def plan(self) -> int:
        return self.task.qty_plan or 0

    @property
    def to_print(self) -> int:
        """Сколько этикеток не хватает до плана."""
        return max(0, self.plan - self.issued)


@dataclass
class AssemblerLabels:
    """Все задачи одного сборщика на день."""

    user: User
    rows: list[TaskLabels] = field(default_factory=list)

    @property
    def to_print(self) -> int:
        return sum(row.to_print for row in self.rows)

    @property
    def hanging(self) -> int:
        return sum(row.hanging for row in self.rows)


def _voided() -> ColumnElement[bool]:
    """Условие «этикетка аннулирована»: списана, так и не побывав на складе."""
    return and_(Unit.status == WRITTEN_OFF, Unit.accepted_at.is_(None))


def _counts(session: Session, task_ids: list[int]) -> dict[int, tuple[int, int]]:
    """task_id -> (напечатано без аннулированных, висит в «Напечатано»)."""
    if not task_ids:
        return {}
    issued: dict[int, int] = {}
    for task_id, count in session.execute(
        select(Unit.task_id, func.count())
        .where(Unit.task_id.in_(task_ids), not_(_voided()))
        .group_by(Unit.task_id)
    ).all():
        if task_id is not None:
            issued[task_id] = int(count)
    hanging: dict[int, int] = {}
    for task_id, count in session.execute(
        select(Unit.task_id, func.count())
        .where(Unit.task_id.in_(task_ids), Unit.status == PRINTED)
        .group_by(Unit.task_id)
    ).all():
        if task_id is not None:
            hanging[task_id] = int(count)
    return {task_id: (issued.get(task_id, 0), hanging.get(task_id, 0)) for task_id in task_ids}


def article_tasks(
    session: Session, *, day: date, site_id: int, user_id: int | None = None
) -> list[Task]:
    """Задачи «сделать артикул» на день и площадку (по сборщику, если задан)."""
    query = select(Task).where(Task.date == day, Task.site_id == site_id, Task.kind == "article")
    if user_id is not None:
        query = query.where(Task.user_id == user_id)
    return list(session.scalars(query.order_by(Task.user_id, Task.id)).all())


def labels_plan(session: Session, *, day: date, site_id: int) -> list[AssemblerLabels]:
    """План печати на день: сборщики, их задачи, что напечатано и что висит."""
    tasks = article_tasks(session, day=day, site_id=site_id)
    counts = _counts(session, [task.id for task in tasks])
    groups: dict[int, AssemblerLabels] = {}
    for task in tasks:
        issued, hanging = counts.get(task.id, (0, 0))
        done = (
            done_count(session, user_id=task.user_id, article_id=task.article_id, day=day)
            if task.article_id
            else 0
        )
        group = groups.setdefault(task.user_id, AssemblerLabels(user=task.user))
        group.rows.append(TaskLabels(task=task, issued=issued, hanging=hanging, done=done))
    return sorted(groups.values(), key=lambda g: (g.user.is_service, g.user.name))


def print_for_tasks(
    session: Session,
    *,
    tasks: list[Task],
    actor: User,
    shift_id: int | None = None,
) -> list[Unit]:
    """Напечатать этикетки под задачи — ровно сколько не хватает до плана."""
    counts = _counts(session, [task.id for task in tasks])
    units: list[Unit] = []
    for task in tasks:
        if task.kind != "article" or task.article_id is None:
            continue
        issued = counts.get(task.id, (0, 0))[0]
        need = max(0, (task.qty_plan or 0) - issued)
        while need > 0:
            chunk = min(need, MAX_PRINT_QTY)
            units += create_printed_units(
                session,
                article_id=task.article_id,
                qty=chunk,
                site_id=task.site_id,
                assembler_id=task.user_id,
                actor_id=actor.id,
                shift_id=shift_id,
                task_id=task.id,
            )
            need -= chunk
    if not units:
        raise UnitError("Печатать нечего: этикетки на план уже напечатаны")
    return units


def void_units(session: Session, *, units: list[Unit], actor: User, note: str = VOID_NOTE) -> int:
    """Аннулировать неиспользованные этикетки («Напечатано» → «Списано»)."""
    voided = 0
    for candidate in units:
        unit = lock_unit_by_id(session, candidate.id)
        if unit is None or unit.status != PRINTED:
            continue
        apply_transition(
            session, unit, WRITTEN_OFF, movement_type=MV_LABEL_VOID, user_id=actor.id, note=note
        )
        voided += 1
    return voided


def hanging_units(session: Session, *, task_ids: list[int]) -> list[Unit]:
    """Этикетки задач, которые напечатаны, но не приняты сканом."""
    if not task_ids:
        return []
    return list(
        session.scalars(
            select(Unit).where(Unit.task_id.in_(task_ids), Unit.status == PRINTED).order_by(Unit.id)
        ).all()
    )


# Пометка в журнале: зеркала сданы кнопкой «Собрал всё», без скана (наверху нет интернета)
BUTTON_ACCEPT_NOTE = "сдано кнопкой «Собрал всё»"


@dataclass
class AssemblerTask:
    """Задача сборщика на экране «Сканер»: сколько висит этикеток и сколько сдано."""

    task: Task
    hanging: int
    done: int


def assembler_tasks(session: Session, *, user_id: int, day: date) -> list[AssemblerTask]:
    """Задачи сборщика на день и более ранние, по которым ещё висят этикетки."""
    hanging_by_task: dict[int, int] = {}
    for task_id, count in session.execute(
        select(Unit.task_id, func.count())
        .join(Task, Task.id == Unit.task_id)
        .where(Task.user_id == user_id, Unit.status == PRINTED)
        .group_by(Unit.task_id)
    ).all():
        if task_id is not None:
            hanging_by_task[int(task_id)] = int(count)
    tasks = list(
        session.scalars(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.kind == "article",
                (Task.date == day) | Task.id.in_(list(hanging_by_task) or [0]),
            )
            .order_by(Task.date, Task.id)
        ).all()
    )
    result = []
    for task in tasks:
        done = (
            done_count(session, user_id=user_id, article_id=task.article_id, day=task.date)
            if task.article_id
            else 0
        )
        result.append(AssemblerTask(task=task, hanging=hanging_by_task.get(task.id, 0), done=done))
    return result


def submit_task(
    session: Session, *, task_id: int, actor: User, shift_id: int | None = None
) -> int:
    """«Собрал всё»: все висящие этикетки задачи — на склад без скана.

    Если сделано не всё, неиспользованные этикетки потом сканируются в «Убрать лишние
    этикетки» — так на складе остаются ровно наклеенные.
    """
    task = session.get(Task, task_id)
    if task is None or task.kind != "article":
        raise UnitError("Задача не найдена")
    if actor.role != ROLE_ADMIN and task.user_id != actor.id:
        raise UnitError("Это задача другого сборщика")
    units = hanging_units(session, task_ids=[task.id])
    if not units:
        raise UnitError("По этой задаче нет напечатанных этикеток — возьмите их у кладовщицы")
    accepted = 0
    for candidate in units:
        unit = lock_unit_by_id(session, candidate.id)
        if unit is None or unit.status != PRINTED:
            continue
        apply_transition(
            session,
            unit,
            IN_STOCK,
            movement_type=MV_ACCEPT,
            user_id=actor.id,
            shift_id=shift_id,
            to_site_id=task.site_id,
            note=BUTTON_ACCEPT_NOTE,
        )
        accepted += 1
    return accepted
