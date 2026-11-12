"""Уведомления «Расхождений» у начальника смены и админа (2026-11-12)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.alerts import discrepancy_counts
from app.services.units import create_printed_units
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import now_utc
from tests.helpers import login, make_article, make_user, site_by_name


def test_old_labels_counted_and_shown(client: TestClient, db: Session) -> None:
    site = site_by_name(db)
    lead = make_user(db, name="Начальник Счётчиков", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    admin = make_user(db, name="Админ Счётчиков", role=ROLE_ADMIN, home_site_id=site.id)
    worker = make_user(db, name="Сборщик Счётчиков", role=ROLE_ASSEMBLER, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_50x100")
    assert discrepancy_counts(db) == {}

    units = create_printed_units(db, article_id=article.id, qty=3, site_id=site.id, assembler_id=worker.id,
                                 actor_id=lead.id)
    for unit in units[:2]:
        unit.made_at = now_utc() - timedelta(hours=30)
    db.commit()
    assert discrepancy_counts(db) == {"/warehouse/printed-old": 2}

    login(client, lead)  # у начальника: счётчик у пункта «Склад» и на вкладке
    page = client.get("/warehouse/printed-old").text
    assert 'title="Расхождения: 2"' in page and '<span class="alert-badge">2</span>' in page

    client.cookies.clear()
    login(client, admin)  # у админа: плитки по группам, счётчик на группе и на плитке
    page = client.get("/").text
    assert "Расхождения" in page and "Смена" in page and 'class="tile-group"' in page
    assert 'title="Висит: 2"' in page and 'title="Расхождения: 2"' in page

    client.cookies.clear()
    login(client, worker)
    assert "alert-badge" not in client.get("/").text
