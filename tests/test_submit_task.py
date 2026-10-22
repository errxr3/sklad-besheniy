"""«Собрал всё»: сборщик сдаёт задачу кнопкой без скана; лишние этикетки убираются сканом."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.task_labels import print_for_tasks
from app.services.tasks import create_task, tasks_progress
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


def test_submit_all_then_remove_unused(client: TestClient, db: Session) -> None:
    site, worker, article, task, units = _task_with_labels(db, 5)
    open_shift(db, worker, site)
    login(client, worker)
    page = client.get("/").text
    assert "Собрал всё (5 шт.)" in page

    token = get_csrf(client, "/")
    response = client.post("/tasks/submit", data={"csrf_token": token, "task_id": str(task.id)})
    assert "На склад записано: 5 шт." in response.text
    db.expire_all()
    assert stock_count(db, article.id, site.id) == 5
    assert tasks_progress(db, user_id=worker.id, day=today_msk()).done == 5

    # Собрал только 3 — кладовщица сканирует 2 неиспользованные этикетки
    keeper = make_user(db, name="Кладовщик", role=ROLE_STOREKEEPER, home_site_id=site.id)
    with TestClient(client.app) as keeper_client:
        login(keeper_client, keeper)
        token = get_csrf(keeper_client, "/stock-entry/remove")
        for unit in units[3:]:
            result = keeper_client.post(
                "/stock-entry/remove/scan",
                json={"code": unit.number, "csrf_token": token},
                headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
            ).json()
            assert result["ok"] is True, result
    db.expire_all()
    assert stock_count(db, article.id, site.id) == 3
    assert {db.get(Unit, u.id).status for u in units[3:]} == {WRITTEN_OFF}  # type: ignore[union-attr]
    assert tasks_progress(db, user_id=worker.id, day=today_msk()).done == 3


def test_submit_requires_shift_and_own_task(client: TestClient, db: Session) -> None:
    site, worker, _, task, _ = _task_with_labels(db, 2)
    other = make_user(db, name="Пётр Сборщик", role=ROLE_ASSEMBLER)
    login(client, worker)
    token = get_csrf(client, "/")
    without_shift = client.post("/tasks/submit", data={"csrf_token": token, "task_id": str(task.id)})
    assert without_shift.status_code == 403

    open_shift(db, other, site)
    with TestClient(client.app) as other_client:
        login(other_client, other)
        other_token = get_csrf(other_client, "/")
        response = other_client.post(
            "/tasks/submit", data={"csrf_token": other_token, "task_id": str(task.id)}
        )
        assert "задача другого сборщика" in response.text
    db.expire_all()
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
    from app.services.task_labels import submit_task

    _, worker, _, task, units = _task_with_labels(db, 2)
    submit_task(db, task_id=task.id, actor=worker, shift_id=None)
    db.commit()
    keeper = make_user(
        db, name="Кладовщица Учит", role=ROLE_STOREKEEPER, home_site_id=site_by_name(db, "Учительская").id
    )
    result = remove_extra(db, code=units[0].number, actor=keeper)
    assert not result.ok
    assert "площадки Коминтерна" in result.message
