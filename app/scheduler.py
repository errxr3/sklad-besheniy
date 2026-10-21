"""Фоновые задачи: автозакрытие смен и выгрузка в гугл-таблицу (SPEC §2, §5.4, §10)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db import session_scope
from app.services.shifts import AUTO_CLOSE_HOUR, AUTO_CLOSE_MINUTE, auto_close_shifts
from app.timeutil import MOSCOW

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


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


def job_sync_sheets() -> None:
    """Выгрузить остатки в гугл-таблицу (если она настроена)."""
    from app.services.sheets import sync_stock_to_sheets

    try:
        with session_scope() as session:
            result = sync_stock_to_sheets(session)
        if result.skipped:
            logger.debug("Выгрузка в гугл-таблицу пропущена", extra={"status": result.message})
        else:
            logger.info("Выгрузка в гугл-таблицу", extra={"status": result.message})
    except Exception:
        logger.exception("Выгрузка в гугл-таблицу не выполнена")


def start_scheduler() -> BackgroundScheduler | None:
    """Запустить планировщик, если он включён настройками."""
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler

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
    scheduler.add_job(
        job_sync_sheets,
        IntervalTrigger(minutes=max(1, settings.google_sync_minutes)),
        id="sync_sheets",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Планировщик запущен")
    return scheduler


def stop_scheduler() -> None:
    """Остановить планировщик."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_running() -> bool:
    """Запущен ли планировщик в этом процессе."""
    return _scheduler is not None and bool(_scheduler.running)
