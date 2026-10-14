"""Фоновые задачи: автозакрытие смен и выгрузка в гугл-таблицу (SPEC §2, §5.4, §10)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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
