"""Задачи завтрашнего дня: сборщик их видит, но сдать раньше срока не может (2026-10-23)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.task_labels import print_for_tasks
from app.services.tasks import create_task
from app.statuses import PRINTED, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def test_tomorrow_task_is_visible_but_not_accepted(client: TestClient, db: Session) -> None:
    site = site_by_name(db)
    lead = make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD)
    worker = make_user(db, name="Магомед", role=ROLE_ASSEMBLER, home_site_id=site.id)
    article = make_article(db, name="Ambient warm 100 krug multi")
    tomorrow = today_msk() + timedelta(days=1)
    task = create_task(
        db, actor=lead, day=tomorrow, site_id=site.id, user_id=worker.id, kind="article",
        article_id=article.id, qty_plan=3,
    )
    units = print_for_tasks(db, tasks=[task], actor=lead)  # кладовщица напечатала заранее
    db.commit()
    open_shift(db, worker, site)
    login(client, worker)

    page = client.get("/").text
    assert "Завтра" in page and "Ambient warm 100 krug multi" in page
    assert "Собрал всё (" not in page  # сдать завтрашнюю задачу кнопкой нельзя

    token = get_csrf(client, "/")
    response = client.post("/tasks/submit", data={"csrf_token": token, "task_id": str(task.id)})
    assert "сдать её можно будет" in response.text

    result = client.post(
        "/scan/accept",
        json={"code": units[0].number, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()
    assert result["ok"] is False and "Задача ещё не началась" in result["message"]
    db.expire_all()
    assert {db.get(Unit, u.id).status for u in units} == {PRINTED}  # type: ignore[union-attr]
