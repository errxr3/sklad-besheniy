"""Этикетка прошлого дня: сборщик сам не сдаёт, только начальник смены сканом (2026-11-10)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.task_labels import print_for_tasks
from app.services.tasks import create_task, done_count
from app.services.units import accept_unit, create_printed_units, stock_count
from app.statuses import IN_STOCK, PRINTED, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import now_utc, today_msk
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _world(db: Session):  # type: ignore[no-untyped-def]
    site = site_by_name(db)
    lead = make_user(db, name="Начальник Старых", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    worker = make_user(db, name="Сборщик Старых", role=ROLE_ASSEMBLER, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_60x160")
    return site, lead, worker, article


def test_yesterday_task_label_only_by_lead(client: TestClient, db: Session) -> None:
    site, lead, worker, article = _world(db)
    task = create_task(db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
                       article_id=article.id, qty_plan=2)
    units = print_for_tasks(db, tasks=[task], actor=lead)
    task.date = today_msk() - timedelta(days=1)  # задача вчерашняя, этикетки не сдали
    db.commit()

    result = accept_unit(db, code=units[0].number, actor=worker)
    assert not result.ok and "задача" in result.detail and "начальнику смены" in result.detail
    assert db.get(Unit, units[0].id).status == PRINTED  # type: ignore[union-attr]
    db.commit()  # снять блокировку строки перед запросом через сайт

    login(client, lead)
    assert "Принять зеркало сканом" in client.get("/warehouse/printed-old").text
    token = get_csrf(client, "/warehouse/printed-old")
    answer = client.post("/scan/accept", json={"code": units[0].number, "csrf_token": token},
                         headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"}).json()
    assert answer["ok"] is True, answer
    db.expire_all()
    unit = db.get(Unit, units[0].id)
    assert unit is not None and unit.status == IN_STOCK and unit.site_id == site.id
    assert stock_count(db, article.id, site.id) == 1
    # Сдано засчитано сборщику этикетки
    assert done_count(db, user_id=worker.id, article_id=article.id, day=today_msk()) == 1


def test_label_printed_ahead_for_today_is_fresh(db: Session) -> None:
    """Напечатали вчера вечером на сегодня — сегодня сборщик сдаёт сам, хоть и прошло больше суток."""
    site, lead, worker, article = _world(db)
    task = create_task(db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
                       article_id=article.id, qty_plan=1)
    units = print_for_tasks(db, tasks=[task], actor=lead)
    units[0].made_at = now_utc() - timedelta(hours=30)
    db.commit()
    assert accept_unit(db, code=units[0].number, actor=worker).ok


def test_old_label_without_task(db: Session) -> None:
    site, lead, worker, article = _world(db)
    units = create_printed_units(db, article_id=article.id, qty=2, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)
    units[0].made_at = now_utc() - timedelta(hours=25)
    db.commit()
    assert not accept_unit(db, code=units[0].number, actor=worker).ok
    assert accept_unit(db, code=units[1].number, actor=worker).ok  # свежая — можно
    assert accept_unit(db, code=units[0].number, actor=lead).ok
