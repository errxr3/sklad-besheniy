"""Сверка остатков с журналом движений (SPEC §8, команда `verify-stock`)."""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.statuses import IN_STOCK


@dataclass
class VerifyReport:
    """Итог сверки."""

    units_checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Расхождений нет."""
        return not self.problems


def rebuild_from_movements(session: Session) -> dict[int, tuple[str, int | None]]:
    """Восстановить статус и площадку каждого зеркала по журналу движений."""
    rows = session.execute(
        select(Movement.unit_id, Movement.to_status, Movement.to_site_id).order_by(
            Movement.created_at, Movement.id
        )
    ).all()
    state: dict[int, tuple[str, int | None]] = {}
    for unit_id, to_status, to_site_id in rows:
        previous = state.get(unit_id)
        site = to_site_id if to_site_id is not None else (previous[1] if previous else None)
        state[unit_id] = (to_status, site)
    return state


def verify_stock(session: Session) -> VerifyReport:
    """Сравнить таблицу `units` с состоянием, восстановленным по журналу."""
    report = VerifyReport()
    rebuilt = rebuild_from_movements(session)
    units = session.scalars(select(Unit)).all()
    report.units_checked = len(units)

    for unit in units:
        restored = rebuilt.get(unit.id)
        if restored is None:
            report.problems.append(f"{unit.number}: нет ни одной записи в журнале движений")
            continue
        status, site_id = restored
        if status != unit.status:
            report.problems.append(
                f"{unit.number}: в таблице статус «{unit.status}», по журналу «{status}»"
            )
        if site_id is not None and site_id != unit.site_id:
            report.problems.append(
                f"{unit.number}: в таблице площадка {unit.site_id}, по журналу {site_id}"
            )

    known = {unit.id for unit in units}
    for unit_id in rebuilt:
        if unit_id not in known:
            report.problems.append(f"зеркало {unit_id} есть в журнале, но отсутствует в таблице units")

    # Сверка агрегатов остатка по (артикул, площадка)
    table_stock = Counter(
        (unit.article_id, unit.site_id) for unit in units if unit.status == IN_STOCK
    )
    by_id = {unit.id: unit for unit in units}
    journal_stock: Counter[tuple[int, int]] = Counter()
    for unit_id, (status, site_id) in rebuilt.items():
        known_unit = by_id.get(unit_id)
        if known_unit is not None and status == IN_STOCK:
            place = site_id if site_id is not None else known_unit.site_id
            journal_stock[(known_unit.article_id, int(place))] += 1
    for key in set(table_stock) | set(journal_stock):
        if table_stock[key] != journal_stock[key]:
            article_id, site_id = key
            report.problems.append(
                f"остаток артикула {article_id} на площадке {site_id}: "
                f"в таблице {table_stock[key]}, по журналу {journal_stock[key]}"
            )
    return report
