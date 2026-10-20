"""Камера: страницы сканирования подключают ZXing, сервер отдаёт его wasm."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.statuses import ROLE_ASSEMBLER
from tests.helpers import login, make_user, open_shift


def test_wasm_served_with_wasm_type(client: TestClient) -> None:
    response = client.get("/static/vendor/zxing_reader.wasm")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/wasm"
    assert response.content[:4] == b"\0asm"


def test_scan_page_uses_zxing_and_versioned_scripts(client: TestClient, db: Session) -> None:
    worker = make_user(db, name="Сборщик", role=ROLE_ASSEMBLER)
    open_shift(db, worker)
    login(client, worker)
    html = client.get("/").text
    assert "/static/vendor/zxing-reader.min.js?v=" in html
    assert "/static/scan.js?v=" in html
    assert "html5-qrcode" not in html
