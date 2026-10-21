"""Артикулы: нормализация, префиксы номеров, цвета подгрупп (SPEC §7, §9, §6.9)."""

import re
from typing import Final

# Кириллические буквы, которые в размерах пишут вместо латинских
_CYRILLIC_TO_LATIN: Final[dict[str, str]] = {
    "х": "x",
    "Х": "x",
    "а": "a",
    "А": "a",
    "е": "e",
    "Е": "e",
    "о": "o",
    "О": "o",
    "с": "c",
    "С": "c",
    "р": "p",
    "Р": "p",
}

_SPACES = re.compile(r"\s+")

# Палитра по умолчанию (SPEC §6.9). Используется, когда в TOP.xlsx цвет темы,
# а не явный RGB: берётся ближайший цвет из этого списка.
DEFAULT_PALETTE: Final[tuple[tuple[str, str], ...]] = (
    ("полотна", "#FFE0C1"),
    ("алюминий black", "#DCE0F8"),
    ("алюминий на ноге", "#FFE5E5"),
    ("Ambient 4000", "#B7E1CD"),
    ("Ambient 3000", "#A4C2F4"),
    ("Ambient 6000", "#F6B26B"),
    ("gorizont/vert", "#DEEFA0"),
)


def normalize_article(name: str) -> str:
    """Нормализованное имя артикула для сравнения и поиска (SPEC §9).

    Убирает крайние пробелы, схлопывает повторные пробелы, заменяет кириллические
    буквы-двойники на латинские и приводит к нижнему регистру.
    """
    text = _SPACES.sub(" ", str(name).strip())
    text = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in text)
    return text.lower()


def prefix_for(name: str) -> str:
    """Префикс номера зеркала по артикулу (SPEC §7).

    `Z` — Zerkalo и bp_Zerkalo, `AT` — Ambient, `A` — Alum, `X` — всё остальное.
    """
    norm = normalize_article(name)
    if norm.startswith(("zerkalo", "bp_zerkalo")):
        return "Z"
    if norm.startswith("ambient"):
        return "AT"
    if norm.startswith("alum"):
        return "A"
    return "X"


def is_subgroup_header(value: str) -> bool:
    """Строка справочника — заголовок подгруппы, а не артикул (SPEC §9).

    Признак: первое слово целиком заглавными буквами — «ПОЛОТНА», «БЕЗРАМНЫЕ КРУГЛЫЕ»,
    а также «ПОЛОТНА без креплений», «АЛЮМИНИЙ на ноге». Артикулы так не начинаются
    («Zerkalo_100x200», «Ambient 90 krug multi»).
    """
    text = str(value).strip()
    if not text:
        return False
    first_word = text.split()[0]
    letters = [ch for ch in first_word if ch.isalpha()]
    if len(letters) < 2:
        return False
    return all(ch.isupper() for ch in letters)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """#RRGGBB или RRGGBB (с возможным альфа-каналом ARGB) в тройку чисел."""
    text = value.lstrip("#")
    if len(text) == 8:  # ARGB
        text = text[2:]
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def nearest_palette_color(rgb: tuple[int, int, int]) -> str:
    """Ближайший цвет из палитры по умолчанию (для цветов темы в TOP.xlsx)."""

    def distance(candidate: str) -> int:
        cr, cg, cb = _hex_to_rgb(candidate)
        return (cr - rgb[0]) ** 2 + (cg - rgb[1]) ** 2 + (cb - rgb[2]) ** 2

    return min((color for _, color in DEFAULT_PALETTE), key=distance)


def apply_tint(rgb: tuple[int, int, int], tint: float) -> tuple[int, int, int]:
    """Осветление (tint > 0) или затемнение (tint < 0) цвета темы, как в Excel."""
    if tint == 0:
        return rgb
    out = []
    for value in rgb:
        if tint > 0:
            out.append(round(value + (255 - value) * tint))
        else:
            out.append(round(value * (1 + tint)))
    return out[0], out[1], out[2]


def format_hex(rgb: tuple[int, int, int]) -> str:
    """Тройка чисел в #RRGGBB."""
    return "#{:02X}{:02X}{:02X}".format(*rgb)
