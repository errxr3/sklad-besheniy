"""Экран «Отгрузка»: как гугл-таблица владельца (2026-10-21)."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Article, Unit
from app.services.shipment_report import CHANNEL_COLUMNS, build_shipment_table
from app.services.shipping import cancel_ship, close_pallet, open_new_pallet, scan_loading
from app.services.tasks import create_task
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import login, make_article, make_user, site_by_name


def _stock(db: Session, article_id: int, site_name: str, qty: int) -> list[Unit]:
    assembler = make_user(db, name=f"Сборщик {site_name} {article_id} {qty}", role=ROLE_ASSEMBLER)
    units = create_printed_units(
        db, article_id=article_id, qty=qty, site_id=site_by_name(db, site_name).id,
        assembler_id=assembler.id, actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()
    return units


def _ship(db: Session, loader, units: list[Unit], channel: str, note: str = "") -> None:
    # У Ozon машина выбирается при открытии канала (2026-10-22)
    truck = note if channel == "Ozon" else ""
    pallet = open_new_pallet(
        db, user=loader, site=site_by_name(db, "Коминтерна"), channel=channel, truck=truck
    )
    for unit in units:
        assert scan_loading(db, code=unit.number, pallet=pallet, actor=loader).ok
    close_pallet(db, pallet_id=pallet.id, user=loader, note="" if truck else note)
    db.commit()


def test_channels_trucks_and_after_columns(db: Session) -> None:
    article = make_article(db, name="Zerkalo_100x200")
    units = _stock(db, article.id, "Коминтерна", 10)
    _stock(db, article.id, "Учительская", 3)
    loader = make_user(db, name="Грузчик", role=ROLE_LOADER)
    lead = make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD)
    create_task(
        db, actor=lead, day=today_msk(), site_id=site_by_name(db, "Коминтерна").id,
        user_id=make_user(db, name="Иван", role=ROLE_ASSEMBLER).id, kind="article",
        article_id=article.id, qty_plan=20,
    )
    db.commit()

    _ship(db, loader, units[:3], "Ozon", "Озон1")
    _ship(db, loader, units[3:5], "Ozon", "Озон3")
    _ship(db, loader, units[5:6], "WB")
    _ship(db, loader, units[6:7], "СГТ")
    # Отменённая отгрузка не считается
    _ship(db, loader, units[7:8], "КГТ")
    assert cancel_ship(db, number=units[7].number, actor=lead).ok
    db.commit()

    table = build_shipment_table(db, day=today_msk())
    row = next(r for r in table.rows if r.name == "Zerkalo_100x200")
    titles = [title for title, _, _ in CHANNEL_COLUMNS]
    by_title = dict(zip(titles, row.channels, strict=True))
    assert by_title == {
        "Озон 1": 3, "КГТ": 0, "СДЭК": 0, "WB": 1, "СГТ": 1, "Озон 2": 0, "Озон 3": 2,
    }
    assert row.before_ship == 10  # остаток до отгрузок
    assert row.before_other == 3
    assert row.tasks_ship == 20
    # Склад общий: Коминтерна 10 + Учительская 3
    assert row.after == 10 + 3 - 7
    assert row.after_with_tasks == 10 + 3 + 20 - 7
    assert table.totals[0] == 3


def test_xlsx_layout_matches_sheet_columns(client: TestClient, db: Session) -> None:
    """XLSX: столбцы как в гугл-таблице, заголовки подгрупп — отдельные строки."""
    import io

    from openpyxl import load_workbook

    db.add(Article(kind="header", name="Полотна", group="Полотна", sort_order=0))
    db.commit()
    article = make_article(db, name="Zerkalo_60x160", sort_order=1)
    _stock(db, article.id, "Коминтерна", 2)
    lead = make_user(db, name="Начальник файла", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    response = client.get("/shipments.xlsx")
    assert response.status_code == 200
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert sheet["B2"].value == "Артикул" and sheet["I2"].value == "Озон 1"
    assert sheet["O2"].value == "Озон 3"
    assert sheet["B4"].value == "Полотна" and sheet["C4"].value is None
    assert sheet["B5"].value == "Zerkalo_60x160"
    assert (sheet["C5"].value, sheet["D5"].value) == (0, 2)
    assert (sheet["S5"].value, sheet["T5"].value) == (2, 2)  # Учительская 0 + Коминтерна 2
    assert sheet["X2"].value is None and sheet["Z2"].value is None


def test_shipments_page_for_lead(client: TestClient, db: Session) -> None:
    lead = make_user(db, name="Начальник отгрузки", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    response = client.get("/shipments")
    assert response.status_code == 200
    for text in ("ДО отгрузок", "ПОСЛЕ отгрузок", "Озон 1", "Озон 3", "склад − отгрузка"):
        assert "общее" not in response.text
        assert text in response.text
    stock = client.get("/stock").text
    assert 'href="/shipments"' in stock  # вкладки раздела «Склад»


def test_only_closed_channels_and_repeats_are_summed(db: Session) -> None:
    """В колонки попадает только закрытый канал; Озон 1 в 8:00 и в 9:00 — сумма в одной колонке."""
    article = make_article(db, name="Zerkalo_80x200")
    units = _stock(db, article.id, "Коминтерна", 6)
    loader = make_user(db, name="Грузчик повторов", role=ROLE_LOADER)

    _ship(db, loader, units[:2], "Ozon", "Озон1")  # «8 утра»
    _ship(db, loader, units[2:3], "Ozon", "Озон1")  # «9 утра» — снова Озон 1
    # Открытый, ещё не закрытый канал WB
    pallet = open_new_pallet(db, user=loader, site=site_by_name(db, "Коминтерна"), channel="WB")
    assert scan_loading(db, code=units[3].number, pallet=pallet, actor=loader).ok
    db.commit()

    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_80x200")
    titles = [title for title, _, _ in CHANNEL_COLUMNS]
    by_title = dict(zip(titles, row.channels, strict=True))
    assert by_title["Озон 1"] == 3
    assert by_title["WB"] == 0  # канал не закрыт
    assert row.before_ship == 6  # до отгрузок — включая открытый канал

    close_pallet(db, pallet_id=pallet.id, user=loader)
    db.commit()
    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_80x200")
    assert dict(zip(titles, row.channels, strict=True))["WB"] == 1
    assert row.after == 2


def test_past_day_uses_stock_from_journal(db: Session) -> None:
    """Прошедший день показывает остатки на конец того дня, а не сегодняшние."""
    from datetime import timedelta

    from app.services.stock import stock_at
    from app.timeutil import now_utc

    article = make_article(db, name="Zerkalo_40x160")
    site = site_by_name(db, "Коминтерна")
    units = _stock(db, article.id, "Коминтерна", 4)
    between = now_utc()
    loader = make_user(db, name="Грузчик истории", role=ROLE_LOADER)
    _ship(db, loader, units[:3], "WB")

    # По журналу: до отгрузки 4 на складе, после — 1, а до приёмки — ничего
    assert stock_at(db, between).get((article.id, site.id)) == 4
    assert stock_at(db, now_utc() + timedelta(seconds=1)).get((article.id, site.id)) == 1
    assert stock_at(db, between - timedelta(days=1)).get((article.id, site.id)) is None

    yesterday = build_shipment_table(db, day=today_msk() - timedelta(days=1))
    row = next(r for r in yesterday.rows if r.name == "Zerkalo_40x160")
    assert (row.before_ship, row.shipped, row.after) == (0, 0, 0)
    assert not yesterday.is_today

    today = build_shipment_table(db, day=today_msk())
    row = next(r for r in today.rows if r.name == "Zerkalo_40x160")
    assert (row.before_ship, row.shipped, row.after) == (4, 3, 1)


def test_done_part_of_tasks_is_not_counted_twice(db: Session) -> None:
    """«Склад + задачи − отгрузка»: задачи сразу, но сделанное (уже на складе) не дважды."""
    from app.services.task_labels import print_for_tasks

    site = site_by_name(db, "Коминтерна")
    article = make_article(db, name="Zerkalo_50x170")
    lead = make_user(db, name="Начальник задач", role=ROLE_SHIFT_LEAD)
    worker = make_user(db, name="Сборщик задач", role=ROLE_ASSEMBLER)
    task = create_task(
        db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
        article_id=article.id, qty_plan=15,
    )
    db.commit()

    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_50x170")
    assert (row.before_ship, row.tasks_ship, row.after_with_tasks) == (0, 15, 15)

    units = print_for_tasks(db, tasks=[task], actor=lead)
    db.commit()
    for unit in units[:5]:
        assert accept_unit(db, code=unit.number, actor=worker).ok
    db.commit()

    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_50x170")
    assert (row.before_ship, row.tasks_ship, row.tasks_ship_done) == (5, 15, 5)
    assert row.after_with_tasks == 15  # 5 на складе + 10 ещё сделают


def test_stock_is_common_tasks_of_both_sites(db: Session) -> None:
    """Склад общий: задача на Учительской тоже входит в «склад + задачи − отгрузка»."""
    other = site_by_name(db, "Учительская")
    article = make_article(db, name="Zerkalo_80x180")
    _stock(db, article.id, "Учительская", 5)
    lead = make_user(db, name="Начальник общий", role=ROLE_SHIFT_LEAD)
    worker = make_user(db, name="Сборщик Учительской", role=ROLE_ASSEMBLER)
    create_task(
        db, actor=lead, day=today_msk(), site_id=other.id, user_id=worker.id, kind="article",
        article_id=article.id, qty_plan=5,
    )
    db.commit()
    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_80x180")
    assert (row.before_other, row.before_ship, row.tasks_other) == (5, 0, 5)
    assert row.after == 5
    assert row.after_with_tasks == 10


def test_open_channel_warned_and_auto_closed(client: TestClient, db: Session) -> None:
    """Незакрытый канал виден на «Отгрузке» и табло; автозакрытие кладёт его в таблицу."""
    from app.services.shipping import auto_close_channels

    article = make_article(db, name="Zerkalo_60x200")
    units = _stock(db, article.id, "Коминтерна", 3)
    loader = make_user(db, name="Забывчивый грузчик", role=ROLE_LOADER)
    pallet = open_new_pallet(db, user=loader, site=site_by_name(db, "Коминтерна"), channel="Ozon")
    for unit in units[:2]:
        assert scan_loading(db, code=unit.number, pallet=pallet, actor=loader).ok
    db.commit()

    lead = make_user(db, name="Начальник каналов", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    assert "Канал Озон 1 (Коминтерна) открыт у Забывчивый грузчик" in client.get("/shipments").text
    assert "Открыт канал Озон 1 у Забывчивый грузчик" in client.get("/board").text
    # Пока канал открыт — отсканированное видно серым «+2», в цифры не входит
    open_row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_60x200")
    assert open_row.channels[0] == 0 and open_row.pending[0] == 2

    assert auto_close_channels(db) == 1
    db.commit()
    row = next(r for r in build_shipment_table(db, day=today_msk()).rows if r.name == "Zerkalo_60x200")
    assert row.channels[0] == 2 and row.pending[0] == 0  # машина сохранилась: «Озон 1»
    page = client.get("/shipments").text
    assert "закрыт автоматически" in page and "открыт у" not in page
