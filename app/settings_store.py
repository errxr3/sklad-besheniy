"""Настройки, меняемые из интерфейса (таблица `settings`)."""

from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict[str, str] = {
    "label_width_mm": "58",
    "label_height_mm": "40",
    "require_mp_barcode": "0",
    "verify_order": "0",
    "ship_cancel_minutes": "10",
}


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


def get_flag(session: Session, key: str) -> bool:
    """Настройка-переключатель."""
    return get_value(session, key).strip() in ("1", "true", "да", "on")


def set_value(session: Session, key: str, value: str) -> None:
    """Записать настройку."""
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.flush()
