"""Сквозной тест браузером (SPEC §12, последний абзац).

Вход сборщика → начало смены (камера подменяется вызовом того же обработчика с кодом)
→ задача видна → печать → скан → прогресс задачи 1/N → вход грузчика → паллета → скан → остаток 0.
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.shifts import make_shift_code
from app.services.tasks import create_task
from app.services.units import stock_count
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import make_article, make_user, site_by_name

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


def _login(page, base: str, label: str, pin: str) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{base}/login")
    page.select_option("#user-select", label=label)
    page.fill("#pin", pin)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _start_shift(page, base: str, code: str) -> None:  # type: ignore[no-untyped-def]
    """Начать смену: вместо камеры зовём её же обработчик с кодом из QR."""
    page.goto(f"{base}/shift/start")
    page.evaluate("code => window.Sklad.shiftCode(code)", code)
    page.wait_for_selector(".scan-result.ok", timeout=10000)
    assert "Смена начата" in page.inner_text("#scan-result")


def test_full_day_in_browser(server: str, db: Session) -> None:
    """Сборщик делает зеркало по задаче, грузчик отгружает его — остаток 0."""
    sync_api = pytest.importorskip("playwright.sync_api")

    site = site_by_name(db)
    code = make_shift_code(db, site)
    article = make_article(db, name="Zerkalo_60x160")
    assembler = make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER, pin="1111")
    make_user(db, name="Пётр Грузчик", role=ROLE_LOADER, pin="2222")
    lead = make_user(db, name="Ольга Начальник", role=ROLE_SHIFT_LEAD, pin="3333")
    create_task(
        db, actor=lead, day=today_msk(), site_id=site.id, user_id=assembler.id,
        kind="article", article_id=article.id, qty_plan=3,
    )
    db.commit()

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])

        # --- Сборщик ---
        worker = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(worker, server, "Иван Сборщик — Сборщик зеркал", "1111")
        assert "Смена не начата" in worker.content()
        _start_shift(worker, server, code)

        worker.goto(f"{server}/")
        assert "Zerkalo_60x160" in worker.inner_text("main")
        assert "0 / 3" in worker.inner_text("main")

        # Печать: та же форма, что и кнопка «Печать» (PDF уходит в новую вкладку)
        worker.goto(f"{server}/make?article_id={article.id}")
        csrf = worker.get_attribute("#print-form input[name=csrf_token]", "value")
        pdf = worker.request.post(
            f"{server}/make/print",
            form={"csrf_token": csrf or "", "article_id": str(article.id), "qty": "1"},
        )
        assert pdf.ok and pdf.headers["content-type"] == "application/pdf"

        db.expire_all()
        number = db.scalar(select(Unit.number).where(Unit.article_id == article.id))
        assert number is not None

        # Скан этикетки — как сканер-клавиатура: номер + Enter
        worker.fill("#scan-input", number)
        worker.press("#scan-input", "Enter")
        worker.wait_for_selector(".scan-result.ok", timeout=10000)
        assert "Zerkalo_60x160" in worker.inner_text("#scan-result")

        worker.goto(f"{server}/")
        assert "1 / 3" in worker.inner_text("main")

        # --- Грузчик ---
        carrier = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        _login(carrier, server, "Пётр Грузчик — Грузчик", "2222")
        _start_shift(carrier, server, code)
        carrier.goto(f"{server}/loading")
        carrier.select_option("#channel", "Ozon")
        carrier.click("button:has-text(\"Новая паллета\")")
        carrier.wait_for_selector("#scan-input")
        carrier.fill("#scan-input", number)
        carrier.press("#scan-input", "Enter")
        carrier.wait_for_selector(".scan-result.ok", timeout=10000)
        assert "На паллете: 1" in carrier.inner_text("#scan-result")

        browser.close()

    db.expire_all()
    assert stock_count(db, article.id, site.id) == 0
