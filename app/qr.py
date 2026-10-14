"""Лист A4 с QR-кодом смены для цеха (SPEC §5.1)."""

import io

import qrcode
from qrcode.image.pil import PilImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.labels import FONT_BOLD, FONT_REGULAR, register_fonts


def qr_png(data: str, box_size: int = 10) -> bytes:
    """QR-код в виде PNG."""
    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    code.add_data(data)
    code.make(fit=True)
    image = code.make_image(image_factory=PilImage, fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_shift_code_pdf(site_name: str, code: str, rotated_at: str = "") -> bytes:
    """Лист A4: крупный QR, название площадки и подпись «Скан для начала смены»."""
    register_fonts()
    buffer = io.BytesIO()
    width, height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Код смены — {site_name}")

    pdf.setFont(FONT_BOLD, 34)
    pdf.drawCentredString(width / 2, height - 35 * mm, site_name)

    pdf.setFont(FONT_REGULAR, 20)
    pdf.drawCentredString(width / 2, height - 48 * mm, "Скан для начала смены")

    size = 120 * mm
    image = ImageReader(io.BytesIO(qr_png(code)))
    pdf.drawImage(
        image,
        (width - size) / 2,
        height - 60 * mm - size,
        width=size,
        height=size,
        preserveAspectRatio=True,
        mask="auto",
    )

    pdf.setFont(FONT_REGULAR, 14)
    pdf.drawCentredString(
        width / 2,
        height - 70 * mm - size,
        "Наведите камеру телефона на экране «Мой день» → «Начать смену»",
    )
    pdf.setFont(FONT_REGULAR, 10)
    footer = f"Код выпущен: {rotated_at}" if rotated_at else "Лист повесить в цеху"
    pdf.drawCentredString(width / 2, 20 * mm, footer)
    pdf.drawCentredString(
        width / 2, 14 * mm, "При утере листа перевыпустите код в «Настройки → Площадки»"
    )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
