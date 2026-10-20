"""Разбор отсканированного кода (SPEC §6)."""

import re
from typing import Final

# Своя этикетка: префикс группы + 7 цифр
OWN_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(Z|A|AT|X)\d{7}$")


def clean_code(raw: str) -> str:
    """Привести отсканированную строку к каноническому виду."""
    return str(raw or "").strip().upper()


def is_own_label(raw: str) -> bool:
    """Это этикетка BLAGO, а не код маркетплейса."""
    return bool(OWN_LABEL_RE.match(clean_code(raw)))
