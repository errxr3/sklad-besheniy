"""SPEC §10: выгрузка остатков в гугл-таблицу (с подменой самой записи)."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.importer import import_top
from app.services import sheets
from app.services.stock import build_stock_table, site_locative
from app.services.units import accept_unit, create_printed_units
from app.settings_store import get_value
from app.statuses import ROLE_ASSEMBLER
from tests.helpers import make_user, site_by_name

TOP_PATH = Path(__file__).resolve().parent.parent / "docs" / "input" / "TOP.xlsx"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[list[str]], str]]:
    """Сделать вид, что гугл-таблица настроена, и перехватить запись."""
    written: list[tuple[list[list[str]], str]] = []

    monkeypatch.setattr(sheets, "is_configured", lambda: True)

    def fake_write(values: list[list[str]], updated_text: str) -> None:
        written.append((values, updated_text))

    monkeypatch.setattr(sheets, "_write_values", fake_write)
    return written


def _stock(db: Session) -> None:
    """Справочник и несколько зеркал на складе."""
    from sqlalchemy import select

    from app.models import Article

    import_top(db, TOP_PATH)
    db.commit()
    article = db.scalar(select(Article).where(Article.name_norm == "zerkalo_100x200"))
    assert article is not None
    assembler = make_user(db, name="Сборщик выгрузки", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=3,
        site_id=site.id,
        assembler_id=assembler.id,
        actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()


def test_skipped_without_configuration(db: Session) -> None:
    """Без ключа задача тихо пропускается и пишет статус."""
    get_settings.cache_clear()
    result = sheets.sync_stock_to_sheets(db)
    db.commit()
    assert result.skipped
    assert not result.ok
    assert "не настроена" in get_value(db, "sheets_last_status")


def test_sync_writes_same_rows_as_screen(
    db: Session, configured: list[tuple[list[list[str]], str]]
) -> None:
    """Лист совпадает построчно с экраном «Остатки» (SPEC §12 п. 19)."""
    _stock(db)
    result = sheets.sync_stock_to_sheets(db)
    db.commit()

    assert result.ok
    values, updated_text = configured[0]
    table = build_stock_table(db)

    assert values[0][0] == "Артикул"
    assert values[0][1:3] == [site.name for site in table.sites]
    assert values[0][3] == f"Задачи на {site_locative(table.sites[0].name)}"
    assert len(values) == table.row_count + 1
    assert [row[0] for row in values[1:]] == [row.name for row in table.rows]
    assert updated_text.startswith("Обновлено: ")

    row = next(row for row in values[1:] if row[0] == "Zerkalo_100х200")
    index = 1 + [site.name for site in table.sites].index("Коминтерна")
    assert row[index] == "3"


def test_sync_records_last_time(db: Session, configured: list) -> None:
    """После удачной выгрузки запоминается время — для плашки «не обновлялась»."""
    _stock(db)
    sheets.sync_stock_to_sheets(db)
    db.commit()
    assert get_value(db, "sheets_last_sync")
    assert sheets.minutes_since_sync(db) == 0


def test_sync_error_does_not_crash(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка Google не роняет приложение, а пишется в статус."""
    _stock(db)
    monkeypatch.setattr(sheets, "is_configured", lambda: True)

    def broken(values: list[list[str]], updated_text: str) -> None:
        raise RuntimeError("гугл недоступен")

    monkeypatch.setattr(sheets, "_write_values", broken)

    result = sheets.sync_stock_to_sheets(db)
    db.commit()
    assert not result.ok
    assert not result.skipped
    assert "гугл недоступен" in get_value(db, "sheets_last_status")
