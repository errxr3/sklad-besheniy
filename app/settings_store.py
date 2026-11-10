"""Настройки, меняемые из интерфейса (таблица `settings`)."""

from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict[str, str] = {
    "label_width_mm": "58",
    "label_height_mm": "40",
    "ship_cancel_minutes": "10",
    "scan_check_mode": "0",
}

SCAN_CHECK_KEY = "scan_check_mode"


def scan_check_on(session: Session) -> bool:
    """Режим проверки сканов: склад не решает по остаткам, всё присланное едет (решение 2026-11-10)."""
    return get_value(session, SCAN_CHECK_KEY) == "1"


def get_value(session: Session, key: str) -> str:
    """Значение настройки или значение по умолчанию."""
    row = session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


def get_int(session: Session, key: str) -> int:
    """Числовая настройка."""
    raw = get_value(session, key)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(float(DEFAULTS.get(key, "0") or 0))


def set_value(session: Session, key: str, value: str) -> None:
    """Записать настройку."""
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.flush()
