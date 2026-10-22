"""Задачи из Excel: сверху сотрудники, слева артикулы TOP (2026-10-22)."""

import json
import re
from datetime import timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task
from app.statuses import ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _book(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _setup(db: Session) -> dict:
    uch = site_by_name(db, "Учительская")
    uch.article_prefixes = "Ambient"
    db.commit()
    return {
        "uch": uch,
        "lead": make_user(db, name="Начальник Уч", role=ROLE_SHIFT_LEAD, home_site_id=uch.id),
        "ilya": make_user(db, name="Илья Васильев", role=ROLE_ASSEMBLER, home_site_id=uch.id),
        "sergey": make_user(db, name="Сергей Беляев", role=ROLE_ASSEMBLER, home_site_id=uch.id),
        "gennady": make_user(db, name="Геннадий Иванов", role=ROLE_ASSEMBLER, home_site_id=uch.id),
        "kirill": make_user(db, name="Кирилл Громов", role=ROLE_ASSEMBLER, home_site_id=uch.id),
        "a1": make_article(db, name="Ambient 200x100 knopka 4000", sort_order=1),
        "a2": make_article(db, name="Ambient 160x60 knopka 3000", sort_order=2),
        "a3": make_article(db, name="Ambient 100 krug multi", sort_order=3),
        "a4": make_article(db, name="Ambient warm 120x50 gorizont multi", sort_order=4),
    }


def _preview(client: TestClient, data: dict, content: bytes, day) -> str:
    token = get_csrf(client, "/tasks/import")
    response = client.post(
        "/tasks/import/preview",
        data={"csrf_token": token, "day": day.isoformat(), "site_id": str(data["uch"].id)},
        files={"file": ("tasks.xlsx", content, "application/octet-stream")},
    )
    assert response.status_code == 200
    return response.text


def test_import_with_check_end_to_end(client: TestClient, db: Session) -> None:
    data = _setup(db)
    day = today_msk() + timedelta(days=1)
    content = _book([
        [None, "Илья. В.", "Сергей Б", "Генадий", "Г.Кирилл", "Неизвестный", "Сергей Б"],
        ["Ambient 200x100 knopka 4000", 3, 5, None, None, 4, None],
        [" Ambient  160x60 knopka 3000 ", None, 10, 7, None, None, 2],
        ["Ambient 100 krug multi", 6, None, None, 5, None, None],
        ["Ambient 55 krug multi", 10, None, None, None, None, None],   # нет в справочнике
        ["Ambient warm 120x50 gorizont multi", None, None, None, None, None, 1],
        ["Заголовок группы", None, None, None, None, None, None],
        ["4k 200x100", 2, None, None, None, None, None],                # не артикул TOP
        ["Ambient 200x80 knopka 4000", "много", None, None, None, None, None]
    ])
    login(client, data["lead"])
    html = _preview(client, data, content, day)
    assert "Ambient 55 krug multi" in html and "Этих артикулов нет в справочнике" in html
    assert "4k 200x100" in html  # названия светодиодов сайт не переводит
    assert "Неизвестный" in html and "не найден" in html
    assert not db.scalars(select(Task)).first()  # до подтверждения ничего не записано

    payload = json.loads(re.search(r'name="payload" value="([^"]+)"', html).group(1).replace("&#34;", '"'))
    selected: dict[int, int] = {}
    for col, block in re.findall(r'<select name="col_(\d+)"(.*?)</select>', html, re.S):
        chosen = re.search(r'<option value="(\d+)" selected', block)
        if chosen:
            selected[int(col)] = int(chosen.group(1))
    # «Неизвестный» остался без сотрудника, остальные подобраны
    people = {data[k].id for k in ("ilya", "sergey", "gennady", "kirill")}
    assert set(selected.values()) == people
    assert len(selected) == 5  # колонок с подобранным человеком пять: «Сергей Б» встретился дважды
    assert len(payload) >= 8

    token = get_csrf(client, "/tasks/import")
    form = {"csrf_token": token, "day": day.isoformat(), "site_id": str(data["uch"].id),
            "payload": json.dumps(payload)}
    form.update({f"col_{c}": str(u) for c, u in selected.items()})
    response = client.post("/tasks/import/apply", data=form, follow_redirects=True)
    assert response.status_code == 200
    assert "Поставлено задач" in response.text

    tasks = {
        (t.user_id, t.article_id): t.qty_plan
        for t in db.scalars(select(Task).where(Task.date == day)).all()
    }
    assert tasks[(data["ilya"].id, data["a1"].id)] == 3
    assert tasks[(data["sergey"].id, data["a1"].id)] == 5
    assert tasks[(data["sergey"].id, data["a2"].id)] == 12  # две колонки «Сергей Б»: 10 + 2
    assert tasks[(data["gennady"].id, data["a2"].id)] == 7
    assert tasks[(data["kirill"].id, data["a3"].id)] == 5
    assert tasks[(data["ilya"].id, data["a3"].id)] == 6
    assert tasks[(data["sergey"].id, data["a4"].id)] == 1
    assert len(tasks) == 7  # «Неизвестный» ничего не получил

    # Второй раз тот же файл: задачи уже стоят, сайт ничего не задваивает и говорит об этом
    again = client.post("/tasks/import/apply", data=form, follow_redirects=True)
    assert "не поставлено" in again.text
    kept = db.scalar(select(Task).where(Task.user_id == data["ilya"].id, Task.article_id == data["a1"].id))
    assert kept is not None and kept.qty_plan == 3


def test_import_refuses_past_day_and_foreign_site(client: TestClient, db: Session) -> None:
    data = _setup(db)
    kom = site_by_name(db, "Коминтерна")
    login(client, data["lead"])
    content = _book([[None, "Илья"], ["Ambient 200x100 knopka 4000", 3]])
    past = _preview(client, data, content, today_msk() - timedelta(days=1))
    assert "прошедшие дни" in past
    token = get_csrf(client, "/tasks/import")
    response = client.post(
        "/tasks/import/preview",
        data={
            "csrf_token": token,
            "day": (today_msk() + timedelta(days=1)).isoformat(),
            "site_id": str(kom.id),
        },
        files={"file": ("tasks.xlsx", content, "application/octet-stream")},
    )
    assert "недоступна" in response.text  # начальник Учительской не ставит задачи Коминтерна


def test_import_bad_file_explained(client: TestClient, db: Session) -> None:
    data = _setup(db)
    login(client, data["lead"])
    html = _preview(client, data, b"not an excel", today_msk() + timedelta(days=1))
    assert "нужен Excel" in html
    blank = _book([[None, "Илья"], ["Ambient 200x100 knopka 4000", None]])
    empty = _preview(client, data, blank, today_msk() + timedelta(days=1))
    assert "нет задач" in empty


def test_upload_never_places_tasks_without_confirmation(client: TestClient, db: Session) -> None:
    """Даже если всё узнано, после загрузки только проверка; ставит задачи кнопка."""
    data = _setup(db)
    day = today_msk() + timedelta(days=1)
    login(client, data["lead"])
    content = _book([
        [None, "Илья Васильев", "Кирилл Громов", "Общее "],
        ["Ambient 200x100 knopka 4000", 4, None, 4],
        ["Ambient 100 krug multi", None, 2, 2],
    ])
    html = _preview(client, data, content, day)
    assert "Поставить задачи" in html
    assert "Общее" not in html  # столбец суммы не считается сотрудником
    assert db.scalars(select(Task)).first() is None
