"""Сквозной тест браузером (SPEC §12, последний абзац).

Вход сборщика → начало смены кнопкой
→ кладовщик печатает этикетки на сборщика по задаче → сборщик сканирует
→ вход грузчика → канал «Озон 2» → скан → закрытие канала → остаток 0.
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.labels import LabelData, build_labels_pdf
from app.models import Unit
from app.services.tasks import create_task
from app.services.units import create_printed_units, label_rows, stock_count
from app.statuses import (
    IN_STOCK,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_SHIFT_LEAD,
    ROLE_STOREKEEPER,
)
from app.timeutil import today_msk
from tests.helpers import make_article, make_user, open_shift, site_by_name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_CANDIDATES = (
    os.environ.get("PLAYWRIGHT_CHROMIUM", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)

pytestmark = pytest.mark.browser


def _chromium() -> str | None:
    """Путь к Chromium, если он есть (иначе Playwright возьмёт свой)."""
    for path in CHROMIUM_CANDIDATES:
        if path and Path(path).exists():
            return path
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server(_prepare_database: str) -> Iterator[str]:
    """Настоящий сервер приложения на свободном порту против тестовой базы."""
    port = _free_port()
    env = dict(
        os.environ,
        DATABASE_URL=_prepare_database,
        SCHEDULER_ENABLED="0",
        SESSION_COOKIE_SECURE="0",
        SECRET_KEY="e2e-secret",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("сервер не поднялся")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def _login(page, base: str, login: str, password: str) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{base}/login")
    page.fill("#login", login)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _start_shift(page, base: str, site_name: str) -> None:  # type: ignore[no-untyped-def]
    """Начать смену кнопкой площадки (QR-кода больше нет)."""
    page.goto(f"{base}/")
    page.click(f"form[action='/shift/start'] button:has-text('{site_name}')")
    page.wait_for_selector("text=На смене с", timeout=10000)


def test_full_day_in_browser(server: str, db: Session) -> None:
    """Сборщик делает зеркало по задаче, грузчик отгружает его — остаток 0."""
    sync_api = pytest.importorskip("playwright.sync_api")

    site = site_by_name(db)
    article = make_article(db, name="Zerkalo_60x160")
    assembler = make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER, pin="1111", login="ivan")
    make_user(db, name="Пётр Грузчик", role=ROLE_LOADER, pin="2222", login="petr")
    lead = make_user(db, name="Ольга Начальник", role=ROLE_SHIFT_LEAD, pin="3333", login="olga")
    make_user(
        db, name="Мария Кладовщик", role=ROLE_STOREKEEPER, pin="4444", home_site_id=site.id, login="maria"
    )
    create_task(
        db, actor=lead, day=today_msk(), site_id=site.id, user_id=assembler.id,
        kind="article", article_id=article.id, qty_plan=3,
    )
    db.commit()

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])

        # --- Сборщик ---
        worker = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(worker, server, "ivan", "1111")
        assert "Смена не начата" in worker.content()
        _start_shift(worker, server, site.name)

        worker.goto(f"{server}/")
        assert "Скан зеркала" in worker.inner_text("main")

        # Печать: кладовщик заранее печатает этикетки на сборщика (PDF уходит в новую вкладку)
        keeper = browser.new_context().new_page()
        _login(keeper, server, "maria", "4444")
        keeper.goto(f"{server}/labels")
        assert "Иван Сборщик" in keeper.inner_text("main")
        form = keeper.locator("form.print-form", has=keeper.locator("input[name=user_id]")).first
        fields = {
            name: form.locator(f"input[name={name}]").get_attribute("value") or ""
            for name in ("csrf_token", "user_id", "day", "site_id")
        }
        pdf = keeper.request.post(f"{server}/labels/print", form=fields)
        assert pdf.ok and pdf.headers["content-type"] == "application/pdf"

        db.expire_all()
        number = db.scalar(
            select(Unit.number).where(Unit.article_id == article.id).order_by(Unit.id).limit(1)
        )
        assert number is not None

        worker.goto(f"{server}/")
        # Скан этикетки — как сканер-клавиатура: номер + Enter
        worker.fill("#scan-input", number)
        worker.press("#scan-input", "Enter")
        worker.wait_for_selector(".scan-result.ok", timeout=10000)
        assert "Zerkalo_60x160" in worker.inner_text("#scan-result")
        # Карточка «Мои задачи» обновляется сама: 1 сдано, «Собрал всё» на оставшиеся 2
        worker.wait_for_selector("#my-tasks:has-text('Собрал всё (2 шт.)')", timeout=10000)
        assert "Сдано на склад: 1" in worker.inner_text("#my-tasks")


        # --- Грузчик ---
        carrier = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(carrier, server, "petr", "2222")
        _start_shift(carrier, server, site.name)
        carrier.goto(f"{server}/loading")
        carrier.click("button.channel-btn:has-text(\"Озон 2\")")
        carrier.wait_for_selector("#scan-input")
        carrier.fill("#scan-input", number)
        carrier.press("#scan-input", "Enter")
        carrier.wait_for_selector(".scan-result.ok", timeout=10000)
        assert "В канале Озон 2: 1" in carrier.inner_text("#scan-result")
        # Машина выбрана при открытии — закрыть без выбора
        carrier.once("dialog", lambda dialog: dialog.accept())
        carrier.click("button:has-text(\"Закрыть канал Озон 2\")")
        carrier.wait_for_selector("text=Канал закрыт", timeout=10000)
        assert "Озон 2" in carrier.inner_text("main")

        browser.close()

    db.expire_all()
    assert stock_count(db, article.id, site.id) == 0


def _write_y4m(path: Path, image: Image.Image, seconds: int = 2, fps: int = 10) -> None:
    """Неподвижный кадр как видео Y4M — Chromium отдаёт его странице вместо камеры."""
    frame = image.convert("YCbCr")
    width, height = frame.size
    y, cb, cr = frame.split()
    half = (width // 2, height // 2)
    planes = y.tobytes() + cb.resize(half).tobytes() + cr.resize(half).tobytes()
    with path.open("wb") as out:
        out.write(f"YUV4MPEG2 W{width} H{height} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(seconds * fps):
            out.write(b"FRAME\n" + planes)


def test_camera_reads_printed_label(server: str, db: Session, tmp_path: Path) -> None:
    """Камера телефона читает напечатанную этикетку: кадр 1280×720, этикетка ~40% ширины."""
    sync_api = pytest.importorskip("playwright.sync_api")
    pdfium = pytest.importorskip("pypdfium2")

    article = make_article(db, name="Zerkalo_100x200")
    worker = make_user(db, name="Сборщик Камера", role=ROLE_ASSEMBLER, pin="4444", login="camera")
    open_shift(db, worker)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=1,
        site_id=site_by_name(db).id,
        assembler_id=worker.id,
        actor_id=worker.id,
    )
    db.commit()
    pdf_bytes = build_labels_pdf([LabelData(*row) for row in label_rows(units)])

    label = pdfium.PdfDocument(pdf_bytes)[0].render(scale=4).to_pil().convert("RGB")
    frame = Image.new("RGB", (1280, 720), (170, 170, 170))
    width = 520
    label = label.resize((width, int(label.height * width / label.width)))
    frame.paste(label, ((1280 - label.width) // 2, (720 - label.height) // 2))
    video = tmp_path / "camera.y4m"
    _write_y4m(video, frame)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=_chromium(),
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-video-capture={video}",
            ],
        )
        page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(page, server, "camera", "4444")
        page.goto(f"{server}/")
        page.click("#camera-btn")
        page.wait_for_selector(".scan-result.ok", timeout=15000)
        assert "Zerkalo_100x200" in page.inner_text("#scan-result")
        assert "Принято" in page.inner_text("#scan-result")
        browser.close()

    db.expire_all()
    status = db.scalar(select(Unit.status).where(Unit.article_id == article.id))
    assert status == IN_STOCK


def test_scan_mode_switch_remembered(server: str, db: Session) -> None:
    """«Сканер / Камера»: в режиме камеры поле не держит фокус; выбор запоминается."""
    sync_api = pytest.importorskip("playwright.sync_api")
    worker = make_user(db, name="Сборщик Режим", role=ROLE_ASSEMBLER, pin="5555", login="mode")
    open_shift(db, worker)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(page, server, "mode", "5555")
        page.goto(f"{server}/")
        assert page.inner_text(".scan-mode button.active") == "Сканер"
        assert page.evaluate("document.activeElement.id") == "scan-input"

        page.click(".scan-mode button[data-mode=camera]")
        assert page.evaluate("document.activeElement.id") != "scan-input"
        page.reload()
        assert page.inner_text(".scan-mode button.active") == "Камера"
        page.click("h1")
        assert page.evaluate("document.activeElement.id") != "scan-input"
        assert "secondary" not in (page.get_attribute("#camera-btn", "class") or "")
        browser.close()
