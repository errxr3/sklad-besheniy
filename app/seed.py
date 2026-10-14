"""Начальные данные: площадки, служебный сборщик, настройки по умолчанию.

Для новой установки их создаёт миграция 0002. Эта же функция идемпотентна и
используется командой `python -m app.cli seed` и тестами.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting, Site, User
from app.statuses import ROLE_ASSEMBLER

SERVICE_ASSEMBLER_NAME = "Остаток на запуск"
SERVICE_PIN_HASH = "*служебный сотрудник, вход запрещён*"

DEFAULT_SITES: tuple[tuple[str, int], ...] = (("Учительская", 1), ("Коминтерна", 2))

DEFAULT_SETTINGS: tuple[tuple[str, str], ...] = (
    ("label_width_mm", "58"),
    ("label_height_mm", "40"),
    ("require_mp_barcode", "0"),
    ("verify_order", "0"),
    ("ship_cancel_minutes", "10"),
)


def apply_seed(session: Session) -> None:
    """Создать то, чего не хватает. Существующие записи не трогаются."""
    for name, order in DEFAULT_SITES:
        if session.scalar(select(Site).where(Site.name == name)) is None:
            session.add(Site(name=name, active=True, sort_order=order, geo_radius_m=300))

    service = session.scalar(select(User).where(User.name == SERVICE_ASSEMBLER_NAME))
    if service is None:
        session.add(
            User(
                name=SERVICE_ASSEMBLER_NAME,
                role=ROLE_ASSEMBLER,
                pin_hash=SERVICE_PIN_HASH,
                active=True,
                is_service=True,
            )
        )

    for key, value in DEFAULT_SETTINGS:
        if session.get(Setting, key) is None:
            session.add(Setting(key=key, value=value))

    session.flush()
