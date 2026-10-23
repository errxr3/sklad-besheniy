"""Этикетки в PDF: чёрная шапка, артикул, штрихкод Code 128 (SPEC §7)."""

import io
from dataclasses import dataclass
from pathlib import Path

from reportlab.graphics.barcode import code128
from reportlab.lib.colors import black, white
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

_fonts_registered = False

# Термопринтеры этикеток печатают точками 203 dpi (0,125 мм). Ширина полоски штрихкода должна
# быть целым числом точек, иначе принтер округляет полоски по-разному и код читается плохо.
PRINTER_DOT = 25.4 / 203 * mm
BAR_DOTS = (5, 4, 3, 2)  # от самой широкой полоски к самой узкой: берём самую широкую, что влезет
QUIET_MODULES = 10  # белое поле слева и справа от кода, без него сканер теряет начало кода
MIN_BARCODE_SHARE = 0.42  # штрихкод занимает не меньше этой доли высоты под шапкой


def register_fonts() -> None:
    """Зарегистрировать шрифты с кириллицей (один раз на процесс)."""
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "DejaVuSans-Bold.ttf")))
    _fonts_registered = True


@dataclass(frozen=True)
class LabelData:
    """Данные одной этикетки."""

    number: str
    article: str
    site: str
    assembler: str
    made_at: str


def _wrap_words(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Перенос по словам. Слово, которое шире строки, остаётся целым."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if not current or pdfmetrics.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _break_long(lines: list[str], font: str, size: float, max_width: float) -> list[str]:
    """Разрезать по буквам строки, которые всё равно не влезают."""
    result: list[str] = []
    for line in lines:
        if pdfmetrics.stringWidth(line, font, size) <= max_width or len(line) < 2:
            result.append(line)
            continue
        chunk = ""
        for char in line:
            if not chunk or pdfmetrics.stringWidth(chunk + char, font, size) <= max_width:
                chunk += char
            else:
                result.append(chunk)
                chunk = char
        if chunk:
            result.append(chunk)
    return result


def _fit_article(
    text: str, max_width: float, max_lines: int = 2, start: float = 15.0, minimum: float = 4.0
) -> tuple[float, list[str]]:
    """Подобрать размер шрифта так, чтобы весь артикул влез в заданное число строк.

    Текст не обрезается и слова не рвутся: шрифт уменьшается, пока название не
    поместится целиком. Разрыв слова по буквам — только если даже минимальный
    шрифт не спасает.
    """
    register_fonts()
    size = max(start, minimum)
    while size > minimum:
        lines = _wrap_words(text, FONT_BOLD, size, max_width)
        widest = max(pdfmetrics.stringWidth(line, FONT_BOLD, size) for line in lines)
        if len(lines) <= max_lines and widest <= max_width:
            return size, lines
        size -= 0.5
    lines = _break_long(_wrap_words(text, FONT_BOLD, minimum, max_width), FONT_BOLD, minimum, max_width)
    return minimum, lines[:max_lines]


def draw_label(pdf: canvas.Canvas, data: LabelData, width_mm: float, height_mm: float) -> None:
    """Нарисовать одну этикетку на текущей странице (по образцу из docs/input)."""
    register_fonts()
    width = width_mm * mm
    height = height_mm * mm
    padding = 2.0 * mm
    content_width = width - 2 * padding

    # 1. Чёрная шапка: «BLAGO» слева, площадка справа
    header_height = max(5.0 * mm, height * 0.15)
    pdf.setFillColor(black)
    pdf.rect(0, height - header_height, width, header_height, stroke=0, fill=1)
    pdf.setFillColor(white)
    header_font = header_height / mm * 2.1
    pdf.setFont(FONT_BOLD, header_font)
    baseline = height - header_height + (header_height - header_font * 0.72) / 2
    pdf.drawString(padding, baseline, "BLAGO")
    site_font = header_font * 0.85
    site_text = data.site
    while pdfmetrics.stringWidth(site_text, FONT_REGULAR, site_font) > content_width * 0.55 and site_font > 4:
        site_font -= 0.3
    pdf.setFont(FONT_REGULAR, site_font)
    pdf.drawRightString(width - padding, baseline, site_text)

    # 2. Нижняя строка: сборщик слева, дата справа
    pdf.setFillColor(black)
    footer_font = max(5.0, min(7.5, height_mm * 0.16))
    footer_baseline = padding * 0.7
    pdf.setFont(FONT_REGULAR, footer_font)
    assembler = f"Сборщик: {data.assembler}"
    max_assembler_width = content_width * 0.62
    while (
        pdfmetrics.stringWidth(assembler, FONT_REGULAR, footer_font) > max_assembler_width
        and footer_font > 4
    ):
        footer_font -= 0.3
        pdf.setFont(FONT_REGULAR, footer_font)
    pdf.drawString(padding, footer_baseline, assembler)
    pdf.drawRightString(width - padding, footer_baseline, data.made_at)

    # 3. Остаток высоты делим между артикулом, штрихкодом и номером под ним
    area_top = height - header_height
    area_bottom = footer_baseline + footer_font + 0.8 * mm
    area_height = area_top - area_bottom

    number_font = max(7.0, min(11.0, height_mm * 0.22))
    number_block = number_font * 1.25
    # Название не должно съедать штрихкод: на две строки артикула остаётся не больше, чем нужно
    article_room = area_height * (1 - MIN_BARCODE_SHARE) - number_block - 0.8 * mm - 1.8 * mm
    article_start = min(height_mm * 0.42, max(6.0, article_room / (2 * 1.12)))
    article_size, lines = _fit_article(data.article, content_width, max_lines=2, start=article_start)
    article_block = len(lines) * article_size * 1.12 + 1.8 * mm
    barcode_height = max(5.0 * mm, area_height - article_block - number_block - 0.8 * mm)

    # Артикул по центру, как в образце
    pdf.setFont(FONT_BOLD, article_size)
    text_y = area_top - 0.9 * mm - article_size
    for index, line in enumerate(lines):
        pdf.drawCentredString(width / 2, text_y - index * article_size * 1.08, line)

    # Штрихкод Code 128 и номер цифрами под ним
    barcode = _barcode(data.number, barcode_height, width)
    number_baseline = area_bottom
    left = round((width - barcode.width) / 2 / PRINTER_DOT) * PRINTER_DOT  # полоски по сетке точек
    barcode.drawOn(pdf, left, number_baseline + number_block)
    pdf.setFont(FONT_BOLD, number_font)
    pdf.drawCentredString(width / 2, number_baseline + number_font * 0.2, data.number)


def _barcode(number: str, bar_height: float, label_width: float) -> code128.Code128:
    """Code 128 с самой широкой полоской из целых точек принтера, которая влезает с белыми полями."""
    modules = code128.Code128(number, barWidth=1, quiet=False).width
    for dots in BAR_DOTS:
        bar_width = dots * PRINTER_DOT
        if (modules + 2 * QUIET_MODULES) * bar_width <= label_width:
            break
    else:
        bar_width = label_width / (modules + 2 * QUIET_MODULES)  # очень узкая этикетка
    return code128.Code128(number, barHeight=bar_height, barWidth=bar_width, humanReadable=False, quiet=False)


def build_labels_pdf(labels: list[LabelData], width_mm: float = 58, height_mm: float = 40) -> bytes:
    """Собрать PDF: одна этикетка на страницу."""
    register_fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width_mm * mm, height_mm * mm))
    pdf.setTitle("Этикетки BLAGO")
    for data in labels:
        draw_label(pdf, data, width_mm, height_mm)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
