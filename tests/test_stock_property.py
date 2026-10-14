"""SPEC §12 п. 5: остаток по таблице зеркал совпадает с восстановленным по журналу."""

import random
from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.units import create_printed_units
from app.services.verify import rebuild_from_movements, verify_stock
from app.statuses import (
    IN_STOCK,
    IN_TRANSIT,
    MV_ACCEPT,
    MV_INVENTORY_FIX,
    MV_LABEL_VOID,
    MV_RETURN_BAD,
    MV_RETURN_OK,
    MV_SHIP,
    MV_SHIP_CANCEL,
    MV_TRANSFER_IN,
    MV_TRANSFER_OUT,
    MV_WRITE_OFF,
    ROLE_ASSEMBLER,
    SHIPPED,
    WRITTEN_OFF,
)
from app.transitions import apply_transition, is_allowed
from tests.helpers import make_article, make_user, site_by_name

# Возможные операции: тип движения и целевой статус
OPERATIONS: tuple[tuple[str, str], ...] = (
    (MV_ACCEPT, IN_STOCK),
    (MV_LABEL_VOID, WRITTEN_OFF),
    (MV_TRANSFER_OUT, IN_TRANSIT),
    (MV_TRANSFER_IN, IN_STOCK),
    (MV_SHIP, SHIPPED),
    (MV_SHIP_CANCEL, IN_STOCK),
    (MV_WRITE_OFF, WRITTEN_OFF),
    (MV_RETURN_OK, IN_STOCK),
    (MV_RETURN_BAD, WRITTEN_OFF),
    (MV_INVENTORY_FIX, IN_STOCK),
    (MV_INVENTORY_FIX, WRITTEN_OFF),
)


def _prepare(db: Session, units_count: int) -> list[Unit]:
    """Создать зеркала в статусе «Напечатано»."""
    worker = make_user(db, name=f"Сборщик {random.randint(1, 10**9)}", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    article = make_article(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=units_count,
        site_id=site.id,
        assembler_id=worker.id,
        actor_id=worker.id,
    )
    db.commit()
    return units


def _apply_sequence(db: Session, units: list[Unit], operations: list[int], user_id: int) -> int:
    """Выполнить последовательность операций, пропуская неразрешённые. Вернуть число применённых."""
    sites = {site.id for site in [site_by_name(db), site_by_name(db, "Учительская")]}
    applied = 0
    for step, code in enumerate(operations):
        unit = units[code % len(units)]
        movement_type, to_status = OPERATIONS[(code // 7) % len(OPERATIONS)]
        if not is_allowed(unit.status, to_status, movement_type):
            continue
        to_site = None
        if movement_type in (MV_TRANSFER_OUT, MV_TRANSFER_IN):
            to_site = sorted(sites)[step % len(sites)]
        apply_transition(
            db,
            unit,
            to_status,
            movement_type=movement_type,
            user_id=user_id,
            to_site_id=to_site,
            ref_id=1 if movement_type == MV_INVENTORY_FIX else None,
        )
        applied += 1
    db.commit()
    return applied


def _stock_from_units(db: Session) -> Counter[tuple[int, int]]:
    """Остаток по таблице зеркал."""
    return Counter(
        (unit.article_id, unit.site_id)
        for unit in db.scalars(select(Unit)).all()
        if unit.status == IN_STOCK
    )


def _stock_from_movements(db: Session) -> Counter[tuple[int, int]]:
    """Остаток, восстановленный по журналу движений."""
    rebuilt = rebuild_from_movements(db)
    units = {unit.id: unit for unit in db.scalars(select(Unit)).all()}
    result: Counter[tuple[int, int]] = Counter()
    for unit_id, (status, site_id) in rebuilt.items():
        if status == IN_STOCK:
            unit = units[unit_id]
            result[(unit.article_id, site_id if site_id is not None else unit.site_id)] += 1
    return result


def test_thousand_random_operations(db: Session) -> None:
    """Тысяча случайных операций: остаток по units равен остатку по movements."""
    generator = random.Random(20261014)
    units = _prepare(db, 40)
    user = make_user(db, name="Оператор 1000", role=ROLE_ASSEMBLER)
    operations = [generator.randrange(0, 10_000) for _ in range(1000)]

    applied = _apply_sequence(db, units, operations, user.id)
    assert applied > 100, "последовательность оказалась вырожденной"

    assert _stock_from_units(db) == _stock_from_movements(db)
    assert verify_stock(db).ok


@given(operations=st.lists(st.integers(min_value=0, max_value=9999), min_size=1, max_size=120))
@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_random_sequences_keep_stock_consistent(db: Session, operations: list[int]) -> None:
    """Любая последовательность операций не рассинхронизирует таблицу и журнал."""
    from sqlalchemy import text

    db.execute(text("ALTER TABLE movements DISABLE TRIGGER USER"))
    db.execute(text("TRUNCATE movements, units RESTART IDENTITY CASCADE"))
    db.execute(text("ALTER TABLE movements ENABLE TRIGGER USER"))
    db.commit()

    units = _prepare(db, 8)
    user = make_user(db, name=f"Оператор {random.randint(1, 10**9)}", role=ROLE_ASSEMBLER)
    _apply_sequence(db, units, operations, user.id)

    assert _stock_from_units(db) == _stock_from_movements(db)
    assert verify_stock(db).ok
