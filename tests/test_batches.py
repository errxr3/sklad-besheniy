"""Партии BLAGO OTGRUZKI и решение «что едет» по остаткам (2026-10-29)."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShipDemand, ShipItem, ShipQuestion, Site, Task, User
from app.services.batches import answer_question, receive_batch, receive_demand
from app.services.tasks import create_task, urgent_tasks
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import now_utc, today_msk
from tests.helpers import make_article, make_user, site_by_name

KOM = "Коминтерна"


def _stock(db: Session, article_id: int, site: Site, worker: User, qty: int) -> None:
    units = create_printed_units(
        db, article_id=article_id, qty=qty, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()


def _line(posting: str, channel: str, minutes_ago: int, qty: int = 1, market: str = "") -> dict:
    market = market or {"Ozon": "ozon", "КГТ": "ozon", "СДЭК": "cdek"}.get(channel, "wb")
    return {
        "market": market, "channel": channel, "posting": posting, "article": "Zerkalo_50x130",
        "qty": qty, "production": KOM, "ordered_at": (now_utc() - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _decisions(db: Session) -> dict[str, str]:
    return {d.posting: d.decision for d in db.scalars(select(ShipDemand)).all()}


def _setup(db: Session) -> tuple[Site, User, User, int]:
    site = site_by_name(db, KOM)
    worker = make_user(db, name="Иван Сборщик", role=ROLE_ASSEMBLER, home_site_id=site.id)
    lead = make_user(db, name="Начальник Ком", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_50x130")
    return site, worker, lead, article.id


def test_priority_old_day_then_channel_then_time(db: Session) -> None:
    """Старые дни раньше, внутри дня СГТ > WB > СДЭК > Ozon, внутри канала — по времени."""
    site, worker, _, article_id = _setup(db)
    _stock(db, article_id, site, worker, 3)
    day = today_msk()
    receive_demand(db, day=day, lines=[
        _line("OZ-OLD", "Ozon", 60 * 30),   # вчерашний Ozon — самый старый
        _line("WB-1", "WB", 30),
        _line("SGT-1", "СГТ", 10),
        _line("SGT-2", "СГТ", 5),
        _line("OZ-NEW", "Ozon", 60),
    ])
    db.commit()
    got = _decisions(db)
    assert got["OZ-OLD"] == "go"
    assert got["SGT-1"] == "go" and got["SGT-2"] == "go"
    assert got["WB-1"] == "no" and got["OZ-NEW"] == "no"


def test_multi_unit_posting_goes_whole_or_not(db: Session) -> None:
    site, worker, _, article_id = _setup(db)
    _stock(db, article_id, site, worker, 2)
    lines = [_line("OZ-3PCS", "Ozon", 30, qty=3), _line("OZ-1", "Ozon", 10)]
    receive_demand(db, day=today_msk(), lines=lines)
    db.commit()
    got = _decisions(db)
    assert got["OZ-3PCS"] == "no"  # целиком не помещается — не едет, частями не отправляем
    assert got["OZ-1"] == "go"


def test_shortage_in_work_asks_lead_and_urgent_task(db: Session) -> None:
    """На складе 10, нужно 15, в работе 30 → вопрос начальнику, «сделаем к 14:00» → едут все 15."""
    site, worker, lead, article_id = _setup(db)
    _stock(db, article_id, site, worker, 10)
    create_task(db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
                article_id=article_id, qty_plan=30)
    db.commit()
    receive_demand(db, day=today_msk(), lines=[_line(f"OZ-{i:02d}", "Ozon", 100 - i) for i in range(15)])
    db.commit()
    got = _decisions(db)
    assert sum(1 for v in got.values() if v == "go") == 10
    assert sum(1 for v in got.values() if v == "ask") == 5
    question = db.scalar(select(ShipQuestion))
    assert question is not None and question.short == 5 and question.stock == 10 and question.in_work == 30
    assert question.channels == "Озон 1"

    due = now_utc() + timedelta(hours=2)
    answer_question(
        db, question_id=question.id, actor=lead, yes=True, assembler_id=worker.id, qty=5, due_at=due
    )
    db.commit()
    assert all(v == "go" for v in _decisions(db).values())
    task = db.scalar(select(Task).where(Task.user_id == worker.id))
    assert task is not None and task.urgent_qty == 5 and task.qty_plan == 30
    urgent = urgent_tasks(db, day=today_msk(), user_id=worker.id)
    assert urgent and urgent[0].left == 5 and not urgent[0].overdue


def test_lead_says_no(db: Session) -> None:
    site, worker, lead, article_id = _setup(db)
    _stock(db, article_id, site, worker, 1)
    create_task(db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
                article_id=article_id, qty_plan=5)
    db.commit()
    receive_demand(db, day=today_msk(), lines=[_line("OZ-A", "Ozon", 20), _line("OZ-B", "Ozon", 10)])
    question = db.scalar(select(ShipQuestion))
    assert question is not None
    answer_question(db, question_id=question.id, actor=lead, yes=False)
    db.commit()
    assert _decisions(db) == {"OZ-A": "go", "OZ-B": "no"}


def test_downloaded_labels_keep_their_mirrors(db: Session) -> None:
    """Этикетки Озон 1 уже скачаны — новый СГТ того же дня их зеркала не отбирает."""
    site, worker, _, article_id = _setup(db)
    _stock(db, article_id, site, worker, 1)
    day = today_msk()
    receive_demand(db, day=day, lines=[_line("OZ-1", "Ozon", 60)])
    receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": "OZ-1", "article": "Zerkalo_50x130", "production": KOM, "codes": ["%10123456"]},
    ])
    receive_demand(db, day=day, lines=[_line("SGT-1", "СГТ", 5)])
    db.commit()
    assert _decisions(db) == {"OZ-1": "go", "SGT-1": "no"}


def test_batch_resend_and_conflicts(db: Session) -> None:
    _setup(db)
    day = today_msk()
    item = {"posting": "0123-0001-1", "article": "Zerkalo_50x130", "production": KOM, "codes": ["%1012 3456"]}
    first = receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон1", items=[item])
    again = receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон1", items=[item])
    other = receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон2", items=[item])
    unknown = receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон2",
                            items=[{"posting": "X-1", "article": "Нет такого", "production": KOM}])
    db.commit()
    assert first.added == 1 and again.updated == 1 and again.added == 0
    assert other.kept == ["0123-0001-1: Озон 1"]  # ждёт в Озон 1 — во вторую партию не переезжает
    assert unknown.unknown == ["X-1 Нет такого"]
    stored = db.scalar(select(ShipItem).where(ShipItem.posting == "0123-0001-1"))
    assert stored is not None and stored.batch.title == "Озон 1" and stored.article_id is not None


def test_urgent_task_shows_to_assembler_and_board(client, db: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.helpers import login, open_shift

    site, worker, lead, article_id = _setup(db)
    create_task(db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
                article_id=article_id, qty_plan=30)
    db.commit()
    receive_demand(db, day=today_msk(), lines=[_line("OZ-1", "Ozon", 5)])
    question = db.scalar(select(ShipQuestion))
    assert question is not None
    answer_question(db, question_id=question.id, actor=lead, yes=True, assembler_id=worker.id, qty=1,
                    due_at=now_utc() - timedelta(minutes=1))
    db.commit()
    open_shift(db, worker, site)
    login(client, worker)
    home = client.get("/").text
    assert "Срочно к" in home and "время вышло" in home
    client.cookies.clear()
    login(client, lead)
    board = client.get(f"/board?site_id={site.id}").text
    assert "Срочные задачи" in board and "просрочено" in board
    assert "срочно 1 шт." in client.get(f"/tasks?site_id={site.id}&day={today_msk().isoformat()}").text


def _item(posting: str, origin: str = "") -> dict:
    return {"posting": posting, "origin": origin, "article": "Zerkalo_50x130", "production": KOM,
            "codes": [f"%{posting}"]}


def test_ozon_new_numbers_after_packing(db: Session) -> None:
    """Ozon при сборке выдал новые номера: погруженный заказ больше не держит зеркала."""
    from app.services.shipping import join_channel, scan_loading

    site, worker, _, article_id = _setup(db)
    loader = make_user(db, name="Грузчик Ком", role=ROLE_LOADER, home_site_id=site.id)
    units = create_printed_units(
        db, article_id=article_id, qty=4, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    day = today_msk()
    receive_demand(db, day=day, lines=[_line("100-1", "Ozon", 60, qty=2), _line("200-1", "Ozon", 10)])
    # Заказ 100-1 из двух штук разбит на два отправления, у каждого своя этикетка
    receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон1",
                  items=[_item("100-1-1", "100-1"), _item("100-1-2", "100-1")])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    for unit, code in zip(units[:2], ("%100-1-1", "%100-1-2"), strict=True):
        assert scan_loading(db, code=unit.number, pallet=pallet, actor=loader, ship_code=code).ok
        db.commit()
    # На складе 2: одно держит 200-1, второе достаётся новому заказу
    receive_demand(db, day=day, lines=[_line("300-1", "Ozon", 5)])
    db.commit()
    got = _decisions(db)
    assert got == {"100-1": "go", "200-1": "go", "300-1": "go"}


def test_batch_without_morning_order_keeps_mirrors(db: Session) -> None:
    """Этикетку скачали без утреннего заказа: зеркало всё равно занято."""
    site, worker, _, article_id = _setup(db)
    _stock(db, article_id, site, worker, 1)
    day = today_msk()
    receive_batch(db, day=day, market="ozon", channel="Ozon", truck="Озон1", items=[_item("900-1")])
    receive_demand(db, day=day, lines=[_line("901-1", "Ozon", 5)])
    db.commit()
    assert _decisions(db)["901-1"] == "no"


def test_cdek_loaded_without_labels_frees_reserve(db: Session) -> None:
    """СДЭК грузят без партии: после погрузки его заказ не держит зеркала второй раз."""
    from app.services.shipping import join_channel, scan_loading

    site, worker, _, article_id = _setup(db)
    loader = make_user(db, name="Грузчик Сдэк", role=ROLE_LOADER, home_site_id=site.id)
    units = create_printed_units(
        db, article_id=article_id, qty=2, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    day = today_msk()
    receive_demand(db, day=day, lines=[_line("CD-1", "СДЭК", 60), _line("OZ-1", "Ozon", 10)])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="СДЭК")
    db.commit()
    assert scan_loading(db, code=units[0].number, pallet=pallet, actor=loader).ok
    db.commit()
    # Осталось одно зеркало: оно у OZ-1, СДЭК уже уехал — новый заказ не едет, OZ-1 едет
    receive_demand(db, day=day, lines=[_line("OZ-2", "Ozon", 5)])
    db.commit()
    assert _decisions(db) == {"CD-1": "go", "OZ-1": "go", "OZ-2": "no"}


def test_answer_all_splits_between_workers(client, db: Session) -> None:  # type: ignore[no-untyped-def]
    """«Сделаем» на все: срочное делится между теми, кто делает артикул, и складывается."""
    from app.services.batches import answer_all
    from app.timeutil import MOSCOW

    site, worker, lead, article_id = _setup(db)
    second = make_user(db, name="Пётр Сборщик", role=ROLE_ASSEMBLER, home_site_id=site.id)
    _stock(db, article_id, site, worker, 2)
    day = today_msk()
    create_task(db, actor=lead, day=day, site_id=site.id, user_id=worker.id, kind="article",
                article_id=article_id, qty_plan=10)
    create_task(db, actor=lead, day=day, site_id=site.id, user_id=second.id, kind="article",
                article_id=article_id, qty_plan=4)
    receive_demand(db, day=day, lines=[_line(f"OZ-{i}", "Ozon", 60 - i) for i in range(8)])
    db.commit()
    due = now_utc().astimezone(MOSCOW).replace(hour=15, minute=0, second=0, microsecond=0)
    assert answer_all(db, actor=lead, due_at=due, site_ids=[site.id]) == (1, 0)
    db.commit()
    tasks = {t.user_id: t.urgent_qty for t in db.scalars(select(Task)).all()}
    # нехватка 6 = 8 − 2; Ивану осталось 8 (2 уже сдал), Петру 4 — больше достаётся Ивану
    assert tasks == {worker.id: 5, second.id: 1}
    assert set(_decisions(db).values()) == {"go"}
    # Добавились заказы: новое срочное прибавляется к несданному, а не затирает его
    receive_demand(db, day=day, lines=[_line("OZ-9", "Ozon", 1)])
    db.commit()
    answer_all(db, actor=lead, due_at=due, site_ids=[site.id])
    db.commit()
    assert sum(t.urgent_qty or 0 for t in db.scalars(select(Task)).all()) == 7


def test_no_second_question_when_all_work_promised(db: Session) -> None:
    """Всё, что в работе, уже обещано: на лишние заказы вопрос не открывается, они не едут."""
    from app.services.batches import answer_all
    from app.timeutil import MOSCOW

    site, worker, lead, article_id = _setup(db)
    day = today_msk()
    create_task(db, actor=lead, day=day, site_id=site.id, user_id=worker.id, kind="article",
                article_id=article_id, qty_plan=3)
    receive_demand(db, day=day, lines=[_line(f"OZ-{i}", "Ozon", 60 - i) for i in range(5)])
    db.commit()
    due = now_utc().astimezone(MOSCOW).replace(hour=15, minute=0, second=0, microsecond=0)
    answer_all(db, actor=lead, due_at=due, site_ids=[site.id])
    db.commit()
    question = db.scalars(select(ShipQuestion)).one()
    assert question.answer == "yes" and question.promised == 3
    assert sorted(_decisions(db).values()) == ["go", "go", "go", "no", "no"]


def test_promised_mirrors_made_are_not_counted_twice(db: Session) -> None:
    """Обещанное сдали на склад: оно теперь остаток, а не ещё одно обещание."""
    from app.services.batches import answer_all
    from app.services.task_labels import print_for_tasks
    from app.timeutil import MOSCOW

    site, worker, lead, article_id = _setup(db)
    day = today_msk()
    task = create_task(db, actor=lead, day=day, site_id=site.id, user_id=worker.id, kind="article",
                       article_id=article_id, qty_plan=3)
    receive_demand(db, day=day, lines=[_line(f"OZ-{i}", "Ozon", 60 - i) for i in range(5)])
    due = now_utc().astimezone(MOSCOW).replace(hour=15, minute=0, second=0, microsecond=0)
    answer_all(db, actor=lead, due_at=due, site_ids=[site.id])
    db.commit()
    for unit in print_for_tasks(db, tasks=[task], actor=lead, shift_id=None):
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    receive_demand(db, day=day, lines=[_line("OZ-9", "Ozon", 1)])
    db.commit()
    assert sorted(_decisions(db).values()) == ["go", "go", "go", "no", "no", "no"]


def test_lead_removes_and_returns_on_batches_page(client, db: Session) -> None:  # type: ignore[no-untyped-def]
    """«Партии»: снять отмеченные с отгрузки и вернуть их обратно с той же страницы."""
    from tests.helpers import get_csrf, login

    site, _, lead, _ = _setup(db)
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1",
                  items=[_item("500-1"), _item("501-1")])
    db.commit()
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "500-1"))
    assert item is not None
    login(client, lead)
    page = client.get("/shipments/batches").text
    assert "Снять отмеченные с отгрузки" in page and "Вернуть отмеченные" not in page
    token = get_csrf(client, "/shipments/batches")
    client.post("/shipments/batches/remove", data={"csrf_token": token, "item_id": [str(item.id)],
                                                   "reason": "брак"})
    db.expire_all()
    assert item.state == "removed"
    page = client.get("/shipments/batches").text
    assert "Вернуть отмеченные в отгрузку" in page and "брак" in page
    client.post("/shipments/batches/return", data={"csrf_token": token, "item_id": [str(item.id)]})
    db.expire_all()
    assert item.state == "wait" and item.removed_reason is None
