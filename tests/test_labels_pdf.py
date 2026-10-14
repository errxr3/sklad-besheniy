"""SPEC §12 п. 15: штрихкод из PDF этикетки распознаётся и равен номеру."""

import pypdfium2
import pytest
import zxingcpp

from app.labels import LabelData, _fit_article, build_labels_pdf


def _read_barcode(pdf_bytes: bytes, page_index: int = 0) -> list[str]:
    """Распознать штрихкоды на странице PDF."""
    document = pypdfium2.PdfDocument(pdf_bytes)
    image = document[page_index].render(scale=6).to_pil().convert("L")
    return [result.text for result in zxingcpp.read_barcodes(image)]


@pytest.mark.parametrize(
    ("number", "article"),
    [
        ("Z0000001", "Zerkalo_100х200"),
        ("AT0012345", "Ambient 200x100 knopka 4000"),
        ("A0000042", "Alum rama 200x100 black"),
        ("X9999999", "Прочее изделие с очень длинным названием на две строки"),
    ],
)
def test_barcode_matches_number(number: str, article: str) -> None:
    """Штрихкод с этикетки читается и совпадает с номером."""
    pdf = build_labels_pdf(
        [LabelData(number, article, "Коминтерна", "Иван Иванов", "14.10.2026")]
    )
    assert _read_barcode(pdf) == [number]


def test_one_label_per_page() -> None:
    """Одна этикетка на страницу, номера не перепутаны."""
    labels = [
        LabelData(f"Z000000{i}", "Zerkalo_60x160", "Учительская", "Пётр", "14.10.2026")
        for i in range(1, 4)
    ]
    pdf = build_labels_pdf(labels)
    document = pypdfium2.PdfDocument(pdf)
    assert len(document) == 3
    for index, label in enumerate(labels):
        assert _read_barcode(pdf, index) == [label.number]


def test_custom_label_size_still_readable() -> None:
    """Этикетка другого размера из настроек тоже читается."""
    pdf = build_labels_pdf(
        [LabelData("Z0000777", "Zerkalo_80x200", "Коминтерна", "Анна", "14.10.2026")],
        width_mm=40,
        height_mm=30,
    )
    assert _read_barcode(pdf) == ["Z0000777"]


def test_cyrillic_text_in_pdf() -> None:
    """Кириллица в PDF не теряется."""
    pdf = build_labels_pdf(
        [LabelData("Z0000002", "Зеркало настенное", "Учительская", "Пётр", "14.10.2026")]
    )
    document = pypdfium2.PdfDocument(pdf)
    text = document[0].get_textpage().get_text_bounded()
    assert "СКЛАД" in text
    assert "Учительская" in text
    # Название переносится на две строки, поэтому проверяем слова по отдельности
    assert "Зеркало" in text
    assert "настенное" in text
    assert "Сборщик: Пётр" in text
    assert "14.10.2026" in text


def test_long_article_is_not_cut() -> None:
    """Длинное название переносится на 2 строки с уменьшением шрифта, но не обрезается."""
    article = "Ambient 200x100 knopka 4000 podsvetka"
    size, lines = _fit_article(article, 54 * 2.834, max_lines=2, start=16)
    assert len(lines) <= 2
    assert " ".join(lines) == article
    assert size < 16
