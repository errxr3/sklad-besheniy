"""Номера зеркал: префикс группы + 7 цифр из одной общей последовательности (SPEC §7)."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import UNIT_NUMBER_SEQUENCE

DIGITS = 7


def format_number(prefix: str, value: int) -> str:
    """Собрать номер: префикс + 7 цифр."""
    return f"{prefix}{value:0{DIGITS}d}"


def next_numbers(session: Session, prefix: str, count: int) -> list[str]:
    """Получить `count` номеров из общей последовательности.

    Номера не повторяются и не переиспользуются независимо от префикса и площадки.
    """
    if count < 1:
        return []
    rows = session.execute(
        text(f"SELECT nextval('{UNIT_NUMBER_SEQUENCE}') FROM generate_series(1, :n)"),
        {"n": count},
    ).scalars()
    return [format_number(prefix, int(value)) for value in rows]
