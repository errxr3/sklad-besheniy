"""Время: храним в UTC, показываем и считаем «сегодня» в Москве."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
UTC_FROM_TIMESTAMP = UTC


def now_utc() -> datetime:
    """Текущий момент в UTC (с таймзоной)."""
    return datetime.now(UTC)


def to_moscow(moment: datetime) -> datetime:
    """Перевести момент в московское время."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(MOSCOW)


def today_msk(moment: datetime | None = None) -> date:
    """Сегодняшняя дата по Москве."""
    return to_moscow(moment or now_utc()).date()


def msk_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Границы московских суток в UTC: [начало, начало следующего дня)."""
    start = datetime.combine(day, time.min, tzinfo=MOSCOW)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def fmt_dt(moment: datetime | None) -> str:
    """Дата и время для интерфейса: 13.10.2026 14:20."""
    if moment is None:
        return ""
    return to_moscow(moment).strftime("%d.%m.%Y %H:%M")


def fmt_time(moment: datetime | None) -> str:
    """Только время: 14:20."""
    if moment is None:
        return ""
    return to_moscow(moment).strftime("%H:%M")


def fmt_date(day: date | None) -> str:
    """Дата для интерфейса: 13.10.2026."""
    if day is None:
        return ""
    return day.strftime("%d.%m.%Y")


def fmt_day_month(day: date) -> str:
    """Короткая дата как в гугл-таблице: 13.10."""
    return day.strftime("%d.%m")


def fmt_hours(hours: float | None) -> str:
    """Часы для людей: 8.5 → «8 ч 30 мин», 0.03 → «2 мин»."""
    if hours is None:
        return "—"
    minutes = round(hours * 60)
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} мин"
    return f"{h} ч {m:02d} мин" if m else f"{h} ч"
