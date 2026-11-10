"""Сдача задачи только сканом; лишние этикетки кладовщица убирает сканом."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.task_labels import print_for_tasks
from app.services.tasks import create_task
from app.services.units import stock_count
from app.statuses import IN_STOCK, PRINTED, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER, WRITTEN_OFF
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def _task_with_labels(db: Session, qty: int = 5):  # type: ignore[no-untyped-def]
    site = site_by_name(db)
    lead = make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD)
    worker = make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_60x160")
    task = create_task(
        db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
        article_id=article.id, qty_plan=qty,
    )
    units = print_for_tasks(db, tasks=[task], actor=lead)
    db.commit()
    return site, worker, article, task, units


def test_no_submit_all_button(client: TestClient, db: Session) -> None:
    """Кнопки «Собрал всё» нет: сдать можно только сканом каждого зеркала (решение владельца 2026-11-10)."""
    site, worker, article, task, units = _task_with_labels(db, 5)
    open_shift(db, worker, site)
    login(client, worker)
    page = client.get("/").text
    assert "Собрал всё" not in page and "/tasks/submit" not in page
    token = get_csrf(client, "/")
    response = client.post("/tasks/submit", data={"csrf_token": token, "task_id": str(task.id)})
    assert response.status_code in (404, 405)
    db.expire_all()
    assert stock_count(db, article.id, site.id) == 0
    assert {u.status for u in db.scalars(select(Unit)).all()} == {PRINTED}


def test_scanned_mirror_cannot_be_removed(client: TestClient, db: Session) -> None:
    """Зеркало, принятое сканом, не убрать как «лишнюю этикетку»."""
    from app.services.units import accept_unit

    site, worker, _, _, units = _task_with_labels(db, 1)
    assert accept_unit(db, code=units[0].number, actor=worker).ok
    db.commit()
    keeper = make_user(db, name="Кладовщик 2", role=ROLE_STOREKEEPER, home_site_id=site.id)
    login(client, keeper)
    token = get_csrf(client, "/stock-entry/remove")
    result = client.post(
        "/stock-entry/remove/scan",
        json={"code": units[0].number, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()
    assert result["ok"] is False
    db.expire_all()
    assert db.get(Unit, units[0].id).status == IN_STOCK  # type: ignore[union-attr]


def test_storekeeper_removes_only_own_site_labels(db: Session) -> None:
    """Кладовщица Учительской не убирает этикетки Коминтерна (2026-10-22)."""
    from app.services.stock_entry import remove_extra

    _, worker, _, task, units = _task_with_labels(db, 2)
    keeper = make_user(
        db, name="Кладовщица Учит", role=ROLE_STOREKEEPER, home_site_id=site_by_name(db, "Учительская").id
    )
    result = remove_extra(db, code=units[0].number, actor=keeper)
    assert not result.ok
    assert "площадки Коминтерна" in result.message


def test_storekeeper_scans_away_unmade_labels(db: Session) -> None:
    """Задачу не доделали — кладовщица сканирует несданные этикетки, они аннулируются (2026-10-22)."""
    from app.services.stock_entry import remove_extra

    site, _, article, _, units = _task_with_labels(db, 3)
    keeper = make_user(db, name="Кладовщица Ком", role=ROLE_STOREKEEPER, home_site_id=site.id)
    result = remove_extra(db, code=units[0].number, actor=keeper)
    db.commit()
    assert result.ok, result.message
    assert "не сделано" in result.detail
    db.refresh(units[0])
    assert units[0].status == WRITTEN_OFF
    assert stock_count(db, article.id, site.id) == 0
    again = remove_extra(db, code=units[0].number, actor=keeper)
    assert not again.ok
