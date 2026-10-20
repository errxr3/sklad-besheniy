"""Сканер-клавиатура при русской раскладке: «Я0000001» читается как Z0000001."""

from app.scan import clean_code, is_own_label


def test_russian_layout_letters_are_restored() -> None:
    assert clean_code("Я0000001") == "Z0000001"
    assert clean_code("ф0000123") == "A0000123"
    assert clean_code("ФЕ0000007\n") == "AT0000007"
    assert clean_code("Ч0000042") == "X0000042"
    assert is_own_label("я0000001")


def test_latin_and_digits_unchanged() -> None:
    assert clean_code(" z0000001 ") == "Z0000001"
    assert clean_code("4603746900777") == "4603746900777"
