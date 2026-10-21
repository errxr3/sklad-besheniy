"""SPEC §12 п. 8: нормализация артикулов и префиксы номеров."""

import pytest

from app.articles import is_subgroup_header, normalize_article, prefix_for


def test_cyrillic_x_equals_latin_x() -> None:
    """Кириллическая «х» в размере приравнивается к латинской."""
    assert normalize_article("Zerkalo_100х200") == normalize_article("Zerkalo_100x200")


def test_extra_spaces_collapse() -> None:
    """Крайние и повторные пробелы не влияют на сравнение."""
    assert normalize_article("  Alum rama   200x100   black ") == normalize_article(
        "Alum rama 200x100 black"
    )


def test_case_insensitive() -> None:
    """Регистр не влияет."""
    assert normalize_article("ZERKALO_60X160") == normalize_article("zerkalo_60x160")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Zerkalo_60x160", "Z"),
        ("zerkalo_100х200_bp", "Z"),
        ("bp_Zerkalo_80x200", "Z"),
        ("Ambient 200x100 knopka 4000", "AT"),
        ("ambient 60x80 gorizont 3000", "AT"),
        ("Alum rama 200x100 black", "A"),
        ("Alum Noga 170x70 black", "A"),
        ("Прочее изделие", "X"),
        ("Shkaf 40x50", "X"),
    ],
)
def test_prefixes(name: str, expected: str) -> None:
    """Префикс номера определяется по артикулу (SPEC §7)."""
    assert prefix_for(name) == expected


def test_ambient_is_at_not_a() -> None:
    """Ambient получает AT, а не A."""
    assert prefix_for("Ambient 200x80 knopka 4000") == "AT"
    assert prefix_for("Alum rama 200x80 black") == "A"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ПОЛОТНА", True),
        ("БЕЗРАМНЫЕ КРУГЛЫЕ", True),
        ("АЛЮМИНИЙ", True),
        ("ПОЛОТНА без креплений", True),
        ("АЛЮМИНИЙ на ноге", True),
        ("Ambient 90 krug multi", False),
        ("Zerkalo_100х200", False),
        ("Alum rama 200x100 black", False),
        ("", False),
    ],
)
def test_subgroup_header_detection(value: str, expected: bool) -> None:
    """Строки целиком заглавными — заголовки подгрупп (SPEC §9)."""
    assert is_subgroup_header(value) is expected
