"""Разбор отсканированного кода (SPEC §6)."""

import re
from typing import Final

# Своя этикетка: префикс группы + 7 цифр
OWN_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(Z|A|AT|X)\d{7}$")


# Сканер в режиме клавиатуры шлёт нажатия клавиш: если на телефоне или компьютере включена
# русская раскладка, вместо Z0000001 приходит «Я0000001». Возвращаем буквы с тех же клавиш.
_RU_TO_EN: Final[dict[int, int]] = str.maketrans(
    "ЙЦУКЕНГШЩЗФЫВАПРОЛДЯЧСМИТЬ",
    "QWERTYUIOPASDFGHJKLZXCVBNM",
)


def clean_code(raw: str) -> str:
    """Привести отсканированную строку к каноническому виду."""
    return str(raw or "").strip().upper().translate(_RU_TO_EN)


def is_own_label(raw: str) -> bool:
    """Это этикетка BLAGO, а не код маркетплейса."""
    return bool(OWN_LABEL_RE.match(clean_code(raw)))
