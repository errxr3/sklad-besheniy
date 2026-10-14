"""SPEC §12 п. 1: все 8 разрешённых переходов работают, остальные отклоняются."""

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.statuses import (
    ALL_STATUSES,
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
    PRINTED,
    SHIPPED,
    WRITTEN_OFF,
)
from app.transitions import (
    ALLOWED_TRANSITIONS,
    TransitionError,
    apply_transition,
    is_allowed,
)
from tests.helpers import make_article, make_user, site_by_name

ALL_TYPES = (
    MV_ACCEPT,
    MV_LABEL_VOID,
    MV_TRANSFER_OUT,
    MV_TRANSFER_IN,
    MV_SHIP,
    MV_SHIP_CANCEL,
    MV_WRITE_OFF,
    MV_RETURN_OK,
    MV_RETURN_BAD,
)


def _unit(db: Session, status: str = PRINTED) -> Unit:
    """Зеркало в нужном статусе (статус задаётся при создании)."""
    site = site_by_name(db)
    article = make_article(db)
    from app.numbering import next_numbers

    number = next_numbers(db, "Z", 1)[0]
    unit = Unit(number=number, article_id=article.id, status=status, site_id=site.id)
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


def test_exactly_eight_transitions() -> None:
    """Словарь переходов содержит ровно 8 строк (SPEC §3)."""
    assert len(ALLOWED_TRANSITIONS) == 8


@pytest.mark.parametrize(
    ("from_status", "to_status", "movement_type"),
    [
        (PRINTED, IN_STOCK, MV_ACCEPT),
        (PRINTED, WRITTEN_OFF, MV_LABEL_VOID),
        (IN_STOCK, IN_TRANSIT, MV_TRANSFER_OUT),
        (IN_TRANSIT, IN_STOCK, MV_TRANSFER_IN),
        (IN_STOCK, SHIPPED, MV_SHIP),
        (IN_STOCK, WRITTEN_OFF, MV_WRITE_OFF),
        (SHIPPED, IN_STOCK, MV_RETURN_OK),
        (SHIPPED, WRITTEN_OFF, MV_RETURN_BAD),
    ],
)
def test_allowed_transition_works(
    db: Session, from_status: str, to_status: str, movement_type: str
) -> None:
    """Каждый разрешённый переход меняет статус и пишет движение."""
    user = make_user(db, name=f"Пользователь {movement_type}")
    unit = _unit(db, from_status)
    apply_transition(db, unit, to_status, movement_type=movement_type, user_id=user.id)
    db.commit()
    db.refresh(unit)
    assert unit.status == to_status
    movements = db.scalars(select(Movement).where(Movement.unit_id == unit.id)).all()
    assert len(movements) == 1
    assert movements[0].from_status == from_status
    assert movements[0].to_status == to_status


def test_ship_cancel_returns_to_stock(db: Session) -> None:
    """Отмена отгрузки возвращает зеркало на склад тем же переходом."""
    user = make_user(db, name="Начальник")
    unit = _unit(db, SHIPPED)
    apply_transition(db, unit, IN_STOCK, movement_type=MV_SHIP_CANCEL, user_id=user.id)
    db.commit()
    db.refresh(unit)
    assert unit.status == IN_STOCK


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        pair
        for pair in itertools.product(ALL_STATUSES, repeat=2)
        if pair not in ALLOWED_TRANSITIONS
    ],
)
def test_forbidden_transitions_rejected(db: Session, from_status: str, to_status: str) -> None:
    """Любой неразрешённый переход отклоняется с понятным текстом."""
    user = make_user(db, name=f"Пользователь {from_status}-{to_status}")
    unit = _unit(db, from_status)
    for movement_type in ALL_TYPES:
        with pytest.raises(TransitionError) as err:
            apply_transition(db, unit, to_status, movement_type=movement_type, user_id=user.id)
        assert "не разрешён" in str(err.value)
        db.rollback()
    db.refresh(unit)
    assert unit.status == from_status
    assert db.scalars(select(Movement).where(Movement.unit_id == unit.id)).all() == []


@pytest.mark.parametrize("from_status", ALL_STATUSES)
def test_inventory_fix_allowed_from_any_status(db: Session, from_status: str) -> None:
    """Исправление по инвентаризации переводит в «На складе» или «Списано» из любого статуса."""
    for target in (IN_STOCK, WRITTEN_OFF):
        if from_status == target:
            assert not is_allowed(from_status, target, MV_INVENTORY_FIX)
            continue
        user = make_user(db, name=f"Инв {from_status}-{target}")
        unit = _unit(db, from_status)
        apply_transition(db, unit, target, movement_type=MV_INVENTORY_FIX, user_id=user.id, ref_id=1)
        db.commit()
        db.refresh(unit)
        assert unit.status == target


@pytest.mark.parametrize("target", [PRINTED, IN_TRANSIT, SHIPPED])
def test_inventory_fix_cannot_set_other_statuses(db: Session, target: str) -> None:
    """Инвентаризация не может поставить произвольный статус."""
    user = make_user(db, name=f"Инв-{target}")
    unit = _unit(db, IN_STOCK)
    with pytest.raises(TransitionError):
        apply_transition(db, unit, target, movement_type=MV_INVENTORY_FIX, user_id=user.id)
    db.rollback()


def test_status_changes_only_with_movement(db: Session) -> None:
    """Статус и запись журнала появляются в одной транзакции."""
    user = make_user(db, name="Приёмщик")
    unit = _unit(db, PRINTED)
    apply_transition(db, unit, IN_STOCK, movement_type=MV_ACCEPT, user_id=user.id)
    # До commit движение уже во flush-состоянии вместе с новым статусом
    assert unit.status == IN_STOCK
    assert db.scalars(select(Movement).where(Movement.unit_id == unit.id)).all()
    db.rollback()
    db.refresh(unit)
    assert unit.status == PRINTED
    assert db.scalars(select(Movement).where(Movement.unit_id == unit.id)).all() == []
