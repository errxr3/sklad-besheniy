"""Артикулы по производствам: Учительская — Ambient, Коминтерна — Zerkalo и Alum (2026-10-21)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.shipment_report import build_shipment_table
from app.services.tasks import TaskError, create_task
from app.statuses import ROLE_ASSEMBLER, ROLE_RETURNS, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import login, make_article, make_user, site_by_name


def _setup(db: Session) -> dict:
    kom = site_by_name(db, "Коминтерна")
    uch = site_by_name(db, "Учительская")
    kom.article_prefixes = "Zerkalo, Alum"
    uch.article_prefixes = "Ambient"
    db.commit()
    return {
        "kom": kom,
        "uch": uch,
        "zerkalo": make_article(db, name="Zerkalo_60x160", sort_order=1),
        "alum": make_article(db, name="Alum rama 60x80 black", sort_order=2),
        "ambient": make_article(db, name="Ambient 90 krug multi", sort_order=3),
        "lead": make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD),
        "worker": make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER),
    }


def _names(client: TestClient, url: str) -> set[str]:
    return {a["name"] for a in client.get(url, headers={"X-Requested-With": "fetch"}).json()["articles"]}


def test_article_search_filtered_by_site(client: TestClient, db: Session) -> None:
    data = _setup(db)
    login(client, data["lead"])
    assert _names(client, f"/api/articles?site_id={data['kom'].id}") == {
        "Zerkalo_60x160", "Alum rama 60x80 black",
    }
    assert _names(client, f"/api/articles?site_id={data['uch'].id}") == {"Ambient 90 krug multi"}
    assert _names(client, f"/api/articles?q=60x&site_id={data['kom'].id}") == {
        "Zerkalo_60x160", "Alum rama 60x80 black",
    }


def test_returns_see_all_articles(client: TestClient, db: Session) -> None:
    """Возвраты всегда на Коминтерна, но артикулы видят любые — Ambient тоже приходит."""
    _setup(db)
    login(client, make_user(db, name="Возвраты", role=ROLE_RETURNS))
    assert "Ambient 90 krug multi" in _names(client, "/api/articles")


def test_task_with_foreign_article_refused(db: Session) -> None:
    data = _setup(db)
    with pytest.raises(TaskError) as err:
        create_task(
            db, actor=data["lead"], day=today_msk(), site_id=data["kom"].id,
            user_id=data["worker"].id, kind="article", article_id=data["ambient"].id, qty_plan=3,
        )
    assert "не делают" in str(err.value)
    task = create_task(
        db, actor=data["lead"], day=today_msk(), site_id=data["uch"].id,
        user_id=data["worker"].id, kind="article", article_id=data["ambient"].id, qty_plan=3,
    )
    assert task.id is not None


def test_shipments_show_who_does_tasks(client: TestClient, db: Session) -> None:
    data = _setup(db)
    petr = make_user(db, name="Пётр Сборщик", role=ROLE_ASSEMBLER)
    for worker, qty in ((data["worker"], 5), (petr, 3)):
        create_task(
            db, actor=data["lead"], day=today_msk(), site_id=data["kom"].id,
            user_id=worker.id, kind="article", article_id=data["zerkalo"].id, qty_plan=qty,
        )
    db.commit()
    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_60x160")
    assert row.tasks_ship == 8
    assert row.tasks_ship_who == "Иван Сборщик — 5, Пётр Сборщик — 3"
    login(client, data["lead"])
    assert "Делают: Иван Сборщик — 5, Пётр Сборщик — 3" in client.get("/shipments").text
