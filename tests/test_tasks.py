"""SPEC §12 п. 13: подсчёт «сделано» и перенос невыполненного остатка задач."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TaskLog, Unit
from app.services.tasks import (
    TaskError,
    copy_remaining,
    create_task,
    delete_task,
    done_count,
    mark_text_task_done,
    plan_fact_report,
    plan_sum_by_site,
    tasks_progress,
    update_task,
)
from app.services.units import create_printed_units
from app.statuses import (
    IN_STOCK,
    MV_ACCEPT,
    MV_WRITE_OFF,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_SHIFT_LEAD,
    WRITTEN_OFF,
)
from app.timeutil import MOSCOW, today_msk
from app.transitions import apply_transition
from tests.helpers import make_article, make_user, site_by_name


def _accept_at(db: Session, unit: Unit, user_id: int, moment: datetime) -> None:
    """Принять зеркало «задним числом» — для проверки границы суток."""
    apply_transition(
        db,
        unit,
        IN_STOCK,
        movement_type=MV_ACCEPT,
        user_id=user_id,
        moment=moment,
    )
    db.commit()


def _units(db: Session, worker, article, qty: int) -> list[Unit]:
    """Напечатать зеркала на сборщика."""
    site = site_by_name(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=qty,
        site_id=site.id,
        assembler_id=worker.id,
        actor_id=worker.id,
    )
    db.commit()
    return units


def test_done_counts_only_own_units(db: Session) -> None:
    """«Сделано» считается только по зеркалам этого сборщика."""
    worker = make_user(db, name="Свой", role=ROLE_ASSEMBLER)
    other = make_user(db, name="Чужой", role=ROLE_ASSEMBLER)
    article = make_article(db)
    today = today_msk()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=12)

    for unit in _units(db, worker, article, 2):
        _accept_at(db, unit, worker.id, moment)
    for unit in _units(db, other, article, 3):
        _accept_at(db, unit, other.id, moment)

    assert done_count(db, user_id=worker.id, article_id=article.id, day=today) == 2
    assert done_count(db, user_id=other.id, article_id=article.id, day=today) == 3


def test_done_counts_only_this_article(db: Session) -> None:
    """«Сделано» считается по своему артикулу."""
    worker = make_user(db, name="Сборщик артикулов", role=ROLE_ASSEMBLER)
    first = make_article(db, name="Zerkalo_60x160", sort_order=1)
    second = make_article(db, name="Zerkalo_80x200", sort_order=2)
    today = today_msk()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=10)

    for unit in _units(db, worker, first, 4):
        _accept_at(db, unit, worker.id, moment)
    for unit in _units(db, worker, second, 1):
        _accept_at(db, unit, worker.id, moment)

    assert done_count(db, user_id=worker.id, article_id=first.id, day=today) == 4
    assert done_count(db, user_id=worker.id, article_id=second.id, day=today) == 1


def test_day_boundary_2359_and_0001_msk(db: Session) -> None:
    """Граница суток по Москве: 23:59 — вчера, 00:01 — сегодня (SPEC §12 п. 13)."""
    worker = make_user(db, name="Ночной сборщик", role=ROLE_ASSEMBLER)
    article = make_article(db)
    day = date(2026, 10, 13)
    next_day = date(2026, 10, 14)

    late = datetime(2026, 10, 13, 23, 59, tzinfo=MOSCOW)
    early = datetime(2026, 10, 14, 0, 1, tzinfo=MOSCOW)

    units = _units(db, worker, article, 3)
    _accept_at(db, units[0], worker.id, late)
    _accept_at(db, units[1], worker.id, early)
    _accept_at(db, units[2], worker.id, early)

    assert done_count(db, user_id=worker.id, article_id=article.id, day=day) == 1
    assert done_count(db, user_id=worker.id, article_id=article.id, day=next_day) == 2


def test_scrapped_units_do_not_count(db: Session) -> None:
    """Зеркало, списанное в брак в тот же день, из «сделано» вычитается."""
    worker = make_user(db, name="Сборщик брака", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник брака", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    today = today_msk()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=9)

    units = _units(db, worker, article, 3)
    for unit in units:
        _accept_at(db, unit, worker.id, moment)
    assert done_count(db, user_id=worker.id, article_id=article.id, day=today) == 3

    apply_transition(
        db,
        units[0],
        WRITTEN_OFF,
        movement_type=MV_WRITE_OFF,
        user_id=lead.id,
        note="бой",
        moment=moment + timedelta(hours=1),
    )
    db.commit()
    assert done_count(db, user_id=worker.id, article_id=article.id, day=today) == 2


def test_task_progress_on_my_day(db: Session) -> None:
    """Прогресс по задаче считается автоматически."""
    worker = make_user(db, name="Сборщик задач", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник задач", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()

    create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=5,
    )
    db.commit()

    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=11)
    for unit in _units(db, worker, article, 2):
        _accept_at(db, unit, worker.id, moment)

    progress = tasks_progress(db, user_id=worker.id, day=today)
    assert progress.plan == 5
    assert progress.done == 2
    assert progress.views[0].percent == 40
    assert progress.views[0].is_done is False


def test_task_done_when_plan_reached(db: Session) -> None:
    """Задача выполнена, когда сделано не меньше плана."""
    worker = make_user(db, name="Сборщик плана", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник плана", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()
    create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=2,
    )
    db.commit()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=11)
    for unit in _units(db, worker, article, 3):
        _accept_at(db, unit, worker.id, moment)

    progress = tasks_progress(db, user_id=worker.id, day=today)
    assert progress.views[0].is_done is True
    assert progress.views[0].percent == 100


def test_copy_remaining_to_tomorrow(db: Session) -> None:
    """Невыполненный остаток переносится на завтра (SPEC §12 п. 13)."""
    worker = make_user(db, name="Сборщик переноса", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник переноса", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()
    tomorrow = today + timedelta(days=1)

    create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=10,
    )
    db.commit()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=13)
    for unit in _units(db, worker, article, 4):
        _accept_at(db, unit, worker.id, moment)

    created = copy_remaining(
        db, actor=lead, source_day=today, target_day=tomorrow, site_id=site.id
    )
    db.commit()
    assert created == 1

    moved = db.scalar(
        select(Task).where(Task.date == tomorrow, Task.user_id == worker.id)
    )
    assert moved is not None
    assert moved.qty_plan == 6


def test_copy_remaining_skips_finished_tasks(db: Session) -> None:
    """Выполненные задачи не переносятся."""
    worker = make_user(db, name="Сборщик выполнил", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник 2", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()
    create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=2,
    )
    db.commit()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=13)
    for unit in _units(db, worker, article, 2):
        _accept_at(db, unit, worker.id, moment)

    created = copy_remaining(
        db, actor=lead, source_day=today, target_day=today + timedelta(days=1), site_id=site.id
    )
    db.commit()
    assert created == 0


def test_text_task_for_loader(db: Session) -> None:
    """Грузчику ставится текстовое поручение, он отмечает его выполненным."""
    loader = make_user(db, name="Грузчик задач", role=ROLE_LOADER)
    lead = make_user(db, name="Начальник грузчика", role=ROLE_SHIFT_LEAD)
    site = site_by_name(db)
    today = today_msk()

    task = create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=loader.id,
        kind="text",
        text="Подготовить паллеты для Ozon",
    )
    db.commit()

    progress = tasks_progress(db, user_id=loader.id, day=today)
    assert progress.views[0].is_done is False

    mark_text_task_done(db, actor=loader, task_id=task.id)
    db.commit()
    progress = tasks_progress(db, user_id=loader.id, day=today)
    assert progress.views[0].is_done is True


def test_article_task_only_for_assembler(db: Session) -> None:
    """Грузчику нельзя поставить задачу с артикулом."""
    loader = make_user(db, name="Грузчик без артикулов", role=ROLE_LOADER)
    lead = make_user(db, name="Начальник 3", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    with pytest.raises(TaskError) as err:
        create_task(
            db,
            actor=lead,
            day=today_msk(),
            site_id=site.id,
            user_id=loader.id,
            kind="article",
            article_id=article.id,
            qty_plan=5,
        )
    assert "только сборщику" in str(err.value)


def test_past_tasks_are_read_only(db: Session) -> None:
    """Задачи за прошедшие дни не ставятся и не правятся."""
    worker = make_user(db, name="Сборщик прошлого", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник 4", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    with pytest.raises(TaskError):
        create_task(
            db,
            actor=lead,
            day=today_msk() - timedelta(days=1),
            site_id=site.id,
            user_id=worker.id,
            kind="article",
            article_id=article.id,
            qty_plan=1,
        )


def test_task_changes_are_logged(db: Session) -> None:
    """Все изменения задач пишутся в журнал задач (SPEC §6.2)."""
    worker = make_user(db, name="Сборщик журнала", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник 5", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()

    task = create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=3,
    )
    db.commit()
    update_task(db, actor=lead, task_id=task.id, qty_plan=7)
    db.commit()
    delete_task(db, actor=lead, task_id=task.id)
    db.commit()

    actions = [row.action for row in db.scalars(select(TaskLog).order_by(TaskLog.id)).all()]
    assert actions == ["создана", "изменена", "удалена"]
    changed = db.scalars(select(TaskLog).where(TaskLog.action == "изменена")).first()
    assert changed is not None
    assert changed.before["qty_plan"] == 3
    assert changed.after["qty_plan"] == 7


def test_plan_sum_by_site(db: Session) -> None:
    """Сумма плана по площадке и дате — для экрана «Остатки» (SPEC §12 п. 20)."""
    worker = make_user(db, name="Сборщик сумм", role=ROLE_ASSEMBLER)
    second = make_user(db, name="Сборщик сумм 2", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник 6", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()

    for user, qty in ((worker, 5), (second, 7)):
        create_task(
            db,
            actor=lead,
            day=today,
            site_id=site.id,
            user_id=user.id,
            kind="article",
            article_id=article.id,
            qty_plan=qty,
        )
    db.commit()

    sums = plan_sum_by_site(db, day=today)
    assert sums[(article.id, site.id)] == 12
    assert plan_sum_by_site(db, day=today + timedelta(days=1)) == {}


def test_plan_fact_report(db: Session) -> None:
    """Отчёт «план/факт» собирает план и факт за период."""
    worker = make_user(db, name="Сборщик отчёта", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник 7", role=ROLE_SHIFT_LEAD)
    article = make_article(db)
    site = site_by_name(db)
    today = today_msk()

    create_task(
        db,
        actor=lead,
        day=today,
        site_id=site.id,
        user_id=worker.id,
        kind="article",
        article_id=article.id,
        qty_plan=4,
    )
    db.commit()
    moment = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW) + timedelta(hours=12)
    for unit in _units(db, worker, article, 3):
        _accept_at(db, unit, worker.id, moment)

    rows = plan_fact_report(db, date_from=today, date_to=today)
    assert len(rows) == 1
    assert rows[0].plan == 4
    assert rows[0].done == 3
    assert rows[0].percent == 75
