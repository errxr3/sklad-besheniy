"""SPEC §12 п. 18: `verify-stock` ловит расхождение таблицы зеркал с журналом."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.services.units import accept_unit, create_printed_units
from app.services.verify import verify_stock
from app.statuses import ROLE_ASSEMBLER
from tests.helpers import make_article, make_user, site_by_name


def _make_stock(db: Session, qty: int = 5) -> None:
    """Напечатать и принять несколько зеркал."""
    worker = make_user(db, name="Сборщик сверки", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    article = make_article(db)
    units = create_printed_units(
        db, article_id=article.id, qty=qty, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )
    db.commit()
    for unit in units[: qty - 1]:
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()


def test_verify_ok_on_consistent_database(db: Session) -> None:
    """На согласованной базе расхождений нет."""
    _make_stock(db)
    report = verify_stock(db)
    assert report.ok, report.problems
    assert report.units_checked == 5


def test_verify_finds_manual_status_change(db: Session) -> None:
    """Прямая правка статуса в обход журнала находится сверкой."""
    _make_stock(db)
    db.execute(text("UPDATE units SET status = 'in_stock' WHERE status = 'printed'"))
    db.commit()

    report = verify_stock(db)
    assert not report.ok
    assert any("по журналу" in problem for problem in report.problems)


def test_verify_finds_site_change(db: Session) -> None:
    """Подменённая площадка тоже находится."""
    _make_stock(db)
    other = site_by_name(db, "Учительская")
    db.execute(text("UPDATE units SET site_id = :sid"), {"sid": other.id})
    db.commit()

    report = verify_stock(db)
    assert not report.ok


def test_cli_exit_codes(db: Session, capsys) -> None:  # type: ignore[no-untyped-def]
    """CLI возвращает 0 на согласованной базе и ненулевой код после порчи данных."""
    _make_stock(db)
    assert cli_main(["verify-stock"]) == 0

    db.execute(text("UPDATE units SET status = 'in_stock' WHERE status = 'printed'"))
    db.commit()
    assert cli_main(["verify-stock"]) == 1
