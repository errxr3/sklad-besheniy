"""Этап 0: схема базы, последовательность номеров, неизменяемость журнала."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import get_engine
from app.main import app
from app.models import Article, Movement, Site, Unit
from app.statuses import IN_STOCK, MV_ACCEPT, PRINTED


def test_health_ok() -> None:
    """/health отвечает 200 и подтверждает доступность базы."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_all_tables_created() -> None:
    """Миграция создала все таблицы из SPEC §8."""
    expected = {
        "sites",
        "users",
        "articles",
        "units",
        "movements",
        "shifts",
        "tasks",
        "task_log",
        "pallets",
        "inventories",
        "inventory_scans",
        "mp_shipments",
        "settings",
        "admin_log",
        "api_keys",
    }
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
        actual = set(rows)
    assert expected <= actual


def test_unit_number_sequence_unique_and_growing() -> None:
    """Номера из общей последовательности не повторяются и растут."""
    with get_engine().connect() as conn:
        numbers = [conn.execute(text("SELECT nextval('unit_number_seq')")).scalar() for _ in range(50)]
    assert len(set(numbers)) == 50
    assert numbers == sorted(numbers)


def _make_unit(db: Session) -> Unit:
    """Площадка, артикул и одно зеркало с движением — для проверки триггера."""
    site = db.query(Site).filter(Site.name == "Коминтерна").one()
    article = Article(name="Zerkalo_60x160", name_norm="zerkalo_60x160", group="Полотна", prefix="Z")
    db.add(article)
    db.flush()
    unit = Unit(number="Z0000001", article_id=article.id, status=PRINTED, site_id=site.id)
    db.add(unit)
    db.flush()
    db.add(
        Movement(
            unit_id=unit.id,
            type=MV_ACCEPT,
            from_status=PRINTED,
            to_status=IN_STOCK,
            to_site_id=site.id,
        )
    )
    db.commit()
    return unit


def test_movements_update_forbidden(db: Session) -> None:
    """UPDATE записи журнала падает на уровне БД (SPEC §12 п. 16)."""
    _make_unit(db)
    with pytest.raises(DBAPIError) as err:
        db.execute(text("UPDATE movements SET note = 'правка'"))
        db.commit()
    assert "нельзя изменять" in str(err.value)
    db.rollback()


def test_movements_delete_forbidden(db: Session) -> None:
    """DELETE записи журнала падает на уровне БД (SPEC §12 п. 16)."""
    _make_unit(db)
    with pytest.raises(DBAPIError) as err:
        db.execute(text("DELETE FROM movements"))
        db.commit()
    assert "нельзя изменять" in str(err.value)
    db.rollback()


def test_movements_truncate_forbidden(db: Session) -> None:
    """TRUNCATE журнала тоже запрещён."""
    _make_unit(db)
    with pytest.raises(DBAPIError):
        db.execute(text("TRUNCATE movements"))
        db.commit()
    db.rollback()


def test_one_open_shift_per_user_index() -> None:
    """Частичные уникальные индексы созданы."""
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")).scalars()
        names = set(rows)
    assert {
        "uq_shifts_one_open_per_user",
        "uq_pallets_one_open_per_loader",
        "uq_inventories_one_active_per_site",
        "ix_units_stock",
    } <= names
