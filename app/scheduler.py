"""Фоновые задачи: автозакрытие смен и выгрузка в гугл-таблицу (SPEC §2, §5.4, §10)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Connection, text

from app.config import get_settings
from app.db import get_engine, session_scope
from app.services.shifts import AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, auto_close_shifts
from app.timeutil import MOSCOW

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
# Сайт работает в нескольких процессах; фоновые задачи — только в одном: тот, кто взял
# эту блокировку PostgreSQL, держит её, пока жив (иначе каналы и смены закрывались бы дважды)
SCHEDULER_LOCK_ID = 7_310_001
_lock_connection: Connection | None = None


def job_auto_close_shifts() -> None:
    """Закрыть смены, которые сотрудники забыли закончить."""
    try:
        with session_scope() as session:
            closed = auto_close_shifts(session)
        if closed:
            logger.info("Автозакрытие смен", extra={"status": f"закрыто {closed}"})
    except Exception:
        logger.exception("Автозакрытие смен не выполнено")


def job_auto_close_channels() -> None:
    """Закрыть каналы погрузки, которые грузчики забыли закрыть, — чтобы отгрузка попала в таблицу."""
    from app.services.shipping import auto_close_channels

    try:
        with session_scope() as session:
            closed = auto_close_channels(session)
        if closed:
            logger.info("Автозакрытие каналов", extra={"status": f"закрыто {closed}"})
    except Exception:
        logger.exception("Автозакрытие каналов не выполнено")


def start_scheduler() -> BackgroundScheduler | None:
    """Запустить планировщик, если он включён настройками."""
    global _scheduler
    global _lock_connection
    settings = get_settings()
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler

    connection = get_engine().connect()
    if not connection.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": SCHEDULER_LOCK_ID}).scalar():
        connection.close()
        logger.info("Планировщик уже работает в другом процессе")
        return None
    connection.commit()
    _lock_connection = connection

    scheduler = BackgroundScheduler(timezone=MOSCOW)
    scheduler.add_job(
        job_auto_close_shifts,
        CronTrigger(hour=AUTO_CLOSE_HOUR, minute=AUTO_CLOSE_MINUTE, timezone=MOSCOW),
        id="auto_close_shifts",
        replace_existing=True,
    )
    scheduler.add_job(
        job_auto_close_channels,
        CronTrigger(hour=23, minute=59, timezone=MOSCOW),
        id="auto_close_channels",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Планировщик запущен")
    return scheduler


def stop_scheduler() -> None:
    """Остановить планировщик."""
    global _scheduler, _lock_connection
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    if _lock_connection is not None:
        _lock_connection.close()  # блокировка снимается вместе с подключением
        _lock_connection = None


def scheduler_running() -> bool:
    """Работает ли планировщик в каком-нибудь процессе сайта."""
    if _scheduler is not None and _scheduler.running:
        return True
    with get_engine().connect() as connection:
        held = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks"
                " WHERE locktype = 'advisory' AND objid = :id AND granted)"
            ),
            {"id": SCHEDULER_LOCK_ID},
        ).scalar()
    return bool(held)
