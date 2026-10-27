"""Ошибки из проверки 2026-10-27: карточка зеркала, инвентаризация администратора, часы табеля."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, User
from app.services.units import create_printed_units
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import fmt_hours
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def test_mirror_card_does_not_preselect_other_assembler(client: TestClient, db: Session) -> None:
    """Зеркало служебного сборщика: в списке никто не выбран, случайно переписать нельзя."""
    lead = make_user(db, name="Начальник Карточки", role=ROLE_SHIFT_LEAD)
    make_user(db, name="Алексей Кузнецов", role=ROLE_ASSEMBLER)
    service = db.scalar(select(User).where(User.name == "Инвентаризация"))
    assert service is not None
    article = make_article(db)
    site = site_by_name(db)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site.id, assembler_id=service.id, actor_id=lead.id
    )[0]
    db.commit()
    login(client, lead)
    html = client.get(f"/mirror/{unit.number}").text
    assert "сейчас: Инвентаризация" in html
    assert '<option value="" selected disabled>' in html
    assert 'selected>Алексей Кузнецов' not in html


def test_admin_chooses_inventory_site(client: TestClient, db: Session) -> None:
    """Администратор без своей площадки может начать инвентаризацию любой площадки."""
    admin = make_user(db, name="Админ Инвент", role=ROLE_ADMIN)
    kom = site_by_name(db, "Коминтерна")
    login(client, admin)
    page = client.get(f"/inventory?site_id={kom.id}").text
    assert "Начать инвентаризацию: Коминтерна" in page
    token = get_csrf(client, f"/inventory?site_id={kom.id}")
    client.post("/inventory/start", data={"csrf_token": token, "site_id": str(kom.id)})
    started = db.scalar(select(Inventory).order_by(Inventory.id.desc()))
    assert started is not None and started.site_id == kom.id


def test_hours_for_people() -> None:
    assert fmt_hours(0.03) == "2 мин"
    assert fmt_hours(8.5) == "8 ч 30 мин"
    assert fmt_hours(9) == "9 ч"
    assert fmt_hours(None) == "—"
