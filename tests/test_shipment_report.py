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
    pallet = open_new_pallet(db, user=loader, site=site_by_name(db, "Коминтерна"), channel=channel)
    for unit in units:
        assert scan_loading(db, code=unit.number, pallet=pallet, actor=loader).ok
    close_pallet(db, pallet_id=pallet.id, user=loader, note=note)
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
        "OZON утро": 3, "КГТ": 0, "СДЭК": 0, "WB": 1, "СГТ": 1, "Ozon 2": 0, "Ozon 3": 2,
    }
    assert row.before_ship == 10  # остаток до отгрузок
    assert row.before_other == 3
    assert row.tasks_ship == 20
    assert row.after == 10 - 7  # склад − отгрузка = остаток сейчас
    assert row.after_with_tasks == 10 + 20 - 7
    assert row.other_with_tasks == 3
    assert row.total == row.after_with_tasks + row.other_with_tasks
    assert table.totals[0] == 3


def test_tsv_layout_matches_sheet_columns(db: Session) -> None:
    """Копирование: C…O и S…Z с пустыми столбцами E, H, W, Y; строки-заголовки пустые."""
    db.add(Article(kind="header", name="Полотна", group="Полотна", sort_order=0))
    db.commit()
    article = make_article(db, name="Zerkalo_60x160", sort_order=1)
    _stock(db, article.id, "Коминтерна", 2)
    table = build_shipment_table(db, day=today_msk())

    before = table.tsv_before().split("\n")
    after = table.tsv_after().split("\n")
    assert before[0] == "" and after[0] == ""
    cells = before[1].split("\t")
    assert len(cells) == 6 + len(CHANNEL_COLUMNS)  # C D E F G H + каналы I…O
    assert cells[:6] == ["0", "2", "", "", "", ""]
    assert after[1].split("\t") == ["2", "2", "", "", "", "0", "", "2"]


def test_shipments_page_for_lead(client: TestClient, db: Session) -> None:
    lead = make_user(db, name="Начальник отгрузки", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    response = client.get("/shipments")
    assert response.status_code == 200
    for text in ("ДО отгрузок", "ПОСЛЕ отгрузок", "OZON утро", "Ozon 3", "склад − отгрузка"):
        assert text in response.text
    stock = client.get("/stock").text
    assert 'href="/shipments"' in stock  # вкладки раздела «Склад»
