"""Задачи по площадкам: сборщики и начальник смены со своей площадкой (2026-10-21)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.tasks import TaskError, create_task, delete_task
from app.statuses import ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import login, make_article, make_user, site_by_name


def _setup(db: Session) -> dict:
    kom = site_by_name(db, "Коминтерна")
    uch = site_by_name(db, "Учительская")
    kom.article_prefixes = ""
    uch.article_prefixes = ""
    db.commit()
    return {
        "kom": kom,
        "uch": uch,
        "article": make_article(db, name="Zerkalo_60x160", sort_order=1),
        "uch_worker": make_user(db, name="Ольга Учительская", role=ROLE_ASSEMBLER, home_site_id=uch.id),
        "kom_worker": make_user(db, name="Иван Коминтерна", role=ROLE_ASSEMBLER, home_site_id=kom.id),
        "free_worker": make_user(db, name="Пётр Без площадки", role=ROLE_ASSEMBLER),
        "lead_uch": make_user(db, name="Начальник Учительская", role=ROLE_SHIFT_LEAD, home_site_id=uch.id),
        "lead_any": make_user(db, name="Начальник общий", role=ROLE_SHIFT_LEAD),
    }


def test_workers_listed_only_on_their_site(client: TestClient, db: Session) -> None:
    data = _setup(db)
    login(client, data["lead_any"])
    page = client.get(f"/tasks?site_id={data['uch'].id}").text
    assert "Ольга Учительская" in page
    assert "Пётр Без площадки" in page
    assert "Иван Коминтерна" not in page
    page = client.get(f"/tasks?site_id={data['kom'].id}").text
    assert "Иван Коминтерна" in page
    assert "Ольга Учительская" not in page


def test_lead_with_site_sees_only_own_site(client: TestClient, db: Session) -> None:
    data = _setup(db)
    login(client, data["lead_uch"])
    # Даже если в адресе чужая площадка — показывается своя
    page = client.get(f"/tasks?site_id={data['kom'].id}").text
    assert "Ольга Учительская" in page
    assert "Иван Коминтерна" not in page
    assert f'<option value="{data["kom"].id}"' not in page


def test_lead_without_site_sees_both(client: TestClient, db: Session) -> None:
    data = _setup(db)
    login(client, data["lead_any"])
    page = client.get("/tasks").text
    assert f'<option value="{data["kom"].id}"' in page
    assert f'<option value="{data["uch"].id}"' in page


def test_lead_cannot_touch_other_site(db: Session) -> None:
    data = _setup(db)
    with pytest.raises(TaskError) as err:
        create_task(
            db, actor=data["lead_uch"], day=today_msk(), site_id=data["kom"].id,
            user_id=data["kom_worker"].id, kind="article", article_id=data["article"].id, qty_plan=3,
        )
    assert "недоступны" in str(err.value)
    task = create_task(
        db, actor=data["lead_any"], day=today_msk(), site_id=data["kom"].id,
        user_id=data["kom_worker"].id, kind="article", article_id=data["article"].id, qty_plan=3,
    )
    with pytest.raises(TaskError):
        delete_task(db, actor=data["lead_uch"], task_id=task.id)


def test_worker_of_other_site_refused(db: Session) -> None:
    data = _setup(db)
    with pytest.raises(TaskError) as err:
        create_task(
            db, actor=data["lead_any"], day=today_msk(), site_id=data["kom"].id,
            user_id=data["uch_worker"].id, kind="article", article_id=data["article"].id, qty_plan=3,
        )
    assert "работает на площадке Учительская" in str(err.value)
