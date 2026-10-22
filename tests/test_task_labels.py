"""Печать этикеток по задачам кладовщиком и уборка висящих этикеток (решение владельца 2026-10-20).

Начальник смены ставит задачи сборщикам → кладовщик заранее печатает этикетки на план,
подписанные сборщиком → сборщик клеит и сканирует готовое зеркало → несделанное убирают,
и скан убранной этикетки больше не проходит.
"""

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.services.task_labels import labels_plan
from app.services.tasks import create_task
from app.statuses import (
    IN_STOCK,
    MV_LABEL_VOID,
    PRINTED,
    ROLE_ASSEMBLER,
    ROLE_SHIFT_LEAD,
    ROLE_STOREKEEPER,
    WRITTEN_OFF,
)
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def _setup(db: Session) -> dict:
    site = site_by_name(db)
    lead = make_user(db, name="Ольга Начальник", role=ROLE_SHIFT_LEAD)
    ivan = make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER)
    petr = make_user(db, name="Пётр Сборщик", role=ROLE_ASSEMBLER)
    keeper = make_user(db, name="Мария Кладовщик", role=ROLE_STOREKEEPER, home_site_id=site.id)
    a60 = make_article(db, name="Zerkalo_60x160", sort_order=1)
    a80 = make_article(db, name="Zerkalo_80x200", sort_order=2)
    day = today_msk()
    t_ivan_60 = create_task(
        db, actor=lead, day=day, site_id=site.id, user_id=ivan.id, kind="article",
        article_id=a60.id, qty_plan=3,
    )
    t_ivan_80 = create_task(
        db, actor=lead, day=day, site_id=site.id, user_id=ivan.id, kind="article",
        article_id=a80.id, qty_plan=2,
    )
    t_petr_60 = create_task(
        db, actor=lead, day=day, site_id=site.id, user_id=petr.id, kind="article",
        article_id=a60.id, qty_plan=4,
    )
    create_task(db, actor=lead, day=day, site_id=site.id, user_id=petr.id, kind="text", text="Уборка")
    db.commit()
    return {
        "site": site, "day": day, "ivan": ivan, "petr": petr, "keeper": keeper,
        "t_ivan_60": t_ivan_60, "t_ivan_80": t_ivan_80, "t_petr_60": t_petr_60,
    }


def _form(token: str, data: dict, **extra: object) -> dict[str, str]:
    form = {"csrf_token": token, "day": data["day"].isoformat(), "site_id": str(data["site"].id)}
    form.update({key: str(value) for key, value in extra.items()})
    return form


def _scan(client: TestClient, token: str, code: str) -> dict:
    return client.post(
        "/scan/accept",
        json={"code": code, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()


def test_storekeeper_prints_per_assembler_by_plan(client: TestClient, db: Session) -> None:
    """Печать — на каждого сборщика отдельно, ровно план, этикетки подписаны своим сборщиком."""
    data = _setup(db)
    login(client, data["keeper"])
    token = get_csrf(client, "/labels")
    page = client.get("/labels").text
    assert "Иван Сборщик" in page and "Пётр Сборщик" in page
    assert "Напечатать всё: Иван Сборщик, 5 шт." in page
    assert "Печать всего на день" not in page
    assert "Допечатать" not in page

    # Без сборщика или задачи печатать нельзя
    nothing = client.post("/labels/print", data=_form(token, data), follow_redirects=False)
    assert "err=" in nothing.headers["location"]

    pdf = client.post("/labels/print", data=_form(token, data, user_id=data["ivan"].id))
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"

    units = db.scalars(select(Unit).order_by(Unit.id)).all()
    assert len(units) == 5
    assert all(u.status == PRINTED and u.assembler_id == data["ivan"].id for u in units)
    by_task: dict[int | None, int] = {}
    for unit in units:
        by_task[unit.task_id] = by_task.get(unit.task_id, 0) + 1
    assert by_task == {data["t_ivan_60"].id: 3, data["t_ivan_80"].id: 2}

    # Повторно печатать нечего — план Ивана уже напечатан
    again = client.post(
        "/labels/print", data=_form(token, data, user_id=data["ivan"].id), follow_redirects=False
    )
    assert again.status_code == 303 and "err=" in again.headers["location"]

    # Одна задача Петра — тоже ровно план
    assert client.post(
        "/labels/print", data=_form(token, data, task_id=data["t_petr_60"].id)
    ).status_code == 200
    assert db.scalar(select(func.count()).select_from(Unit)) == 9
    assert "Всё напечатано" in client.get("/labels").text


def test_storekeeper_sees_only_own_site(client: TestClient, db: Session) -> None:
    """Кладовщик Учительской не видит и не печатает задачи Коминтерна."""
    data = _setup(db)
    other_site = site_by_name(db, "Учительская")
    keeper = make_user(db, name="Кладовщик Учительской", role=ROLE_STOREKEEPER, home_site_id=other_site.id)
    login(client, keeper)
    token = get_csrf(client, "/labels")
    page = client.get(f"/labels?site_id={data['site'].id}").text
    assert "Иван Сборщик" not in page
    assert "Учительская" in page
    refused = client.post(
        "/labels/print",
        data=_form(token, data, task_id=data["t_ivan_60"].id),
        follow_redirects=False,
    )
    assert "err=" in refused.headers["location"]
    assert db.scalar(select(func.count()).select_from(Unit)) == 0


def test_storekeeper_without_site_gets_message(client: TestClient, db: Session) -> None:
    keeper = make_user(db, name="Кладовщик без площадки", role=ROLE_STOREKEEPER)
    login(client, keeper)
    assert "Не указана ваша площадка" in client.get("/labels").text


def test_assembler_scans_then_hanging_labels_are_removed(client: TestClient, db: Session) -> None:
    """Сборщик сканирует сделанное; висящие убираются, их скан больше не проходит."""
    data = _setup(db)
    with TestClient(client.app) as keeper_client:
        login(keeper_client, data["keeper"])
        token = get_csrf(keeper_client, "/labels")
        assert keeper_client.post(
            "/labels/print", data=_form(token, data, task_id=data["t_ivan_60"].id)
        ).status_code == 200
    numbers = [u.number for u in db.scalars(select(Unit).order_by(Unit.id)).all()]

    # Сборщик сделал 2 из 3
    open_shift(db, data["ivan"], data["site"])
    login(client, data["ivan"])
    worker_token = get_csrf(client, "/")
    assert _scan(client, worker_token, numbers[0])["ok"] is True
    assert _scan(client, worker_token, numbers[1])["ok"] is True
    assert "Задачи на сегодня" not in client.get("/").text  # у сборщика только сканер

    # Кладовщик убирает висящую этикетку задачи
    with TestClient(client.app) as keeper_client:
        login(keeper_client, data["keeper"])
        token = get_csrf(keeper_client, "/labels")
        response = keeper_client.post(
            "/labels/void", data=_form(token, data, task_id=data["t_ivan_60"].id), follow_redirects=False
        )
        assert response.status_code == 303
        assert "ok=" in response.headers["location"]

    db.expire_all()
    statuses = {u.number: u.status for u in db.scalars(select(Unit)).all()}
    assert statuses[numbers[0]] == IN_STOCK
    assert statuses[numbers[1]] == IN_STOCK
    assert statuses[numbers[2]] == WRITTEN_OFF
    voids = db.scalars(select(Movement).where(Movement.type == MV_LABEL_VOID)).all()
    assert len(voids) == 1 and voids[0].from_status == PRINTED

    # Скан убранной этикетки не проходит
    refused = _scan(client, worker_token, numbers[2])
    assert refused["ok"] is False
    assert "Списано" in refused["message"]

    # До плана снова не хватает одной этикетки
    plan = labels_plan(db, day=data["day"], site_id=data["site"].id)
    row = next(r for g in plan for r in g.rows if r.task.id == data["t_ivan_60"].id)
    assert (row.issued, row.hanging, row.done, row.to_print) == (2, 0, 2, 1)


def test_void_all_hanging_for_assembler(client: TestClient, db: Session) -> None:
    """«Убрать висящие» по сборщику не трогает этикетки других сборщиков."""
    data = _setup(db)
    login(client, data["keeper"])
    token = get_csrf(client, "/labels")
    for worker in ("ivan", "petr"):
        client.post("/labels/print", data=_form(token, data, user_id=data[worker].id))
    client.post("/labels/void", data=_form(token, data, user_id=data["ivan"].id))
    db.expire_all()
    ivan_units = db.scalars(select(Unit).where(Unit.assembler_id == data["ivan"].id)).all()
    petr_units = db.scalars(select(Unit).where(Unit.assembler_id == data["petr"].id)).all()
    assert {u.status for u in ivan_units} == {WRITTEN_OFF}
    assert {u.status for u in petr_units} == {PRINTED}


def test_void_single_unit_from_printed_list(client: TestClient, db: Session) -> None:
    """Из списка «Напечатано больше суток» этикетку можно убрать по одной; принятую — нельзя."""
    data = _setup(db)
    login(client, data["keeper"])
    token = get_csrf(client, "/labels")
    client.post("/labels/print", data=_form(token, data, task_id=data["t_ivan_80"].id))
    first, second = db.scalars(select(Unit).order_by(Unit.id)).all()

    # Убирать по одной из списка «Напечатано больше суток» может начальник смены
    lead = make_user(db, name="Начальник списка", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    token = get_csrf(client, "/warehouse/printed-old")
    form = {"csrf_token": token, "number": first.number}
    ok = client.post("/labels/void-unit", data=form, follow_redirects=False)
    assert "ok=" in ok.headers["location"]
    assert ok.headers["location"].startswith("/warehouse/printed-old")

    db.expire_all()
    assert db.get(Unit, first.id).status == WRITTEN_OFF
    # Уже убранную повторно убрать нельзя
    repeat = client.post("/labels/void-unit", data=form, follow_redirects=False)
    assert "err=" in repeat.headers["location"]
    assert db.get(Unit, second.id).status == PRINTED


def test_storekeeper_cannot_manage_tasks_or_accept(client: TestClient, db: Session) -> None:
    """Кладовщик печатает, но не ставит задачи и не принимает зеркала сканом."""
    data = _setup(db)
    login(client, data["keeper"])
    token = get_csrf(client, "/labels")
    for path in ("/tasks", "/board", "/stock", "/mirror", "/warehouse/printed-old"):
        assert client.get(path).status_code == 403, path
    response = client.post(
        "/scan/accept",
        json={"code": "Z0000001", "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
