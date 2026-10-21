"""Экран «Отгрузка» — повторяет гугл-таблицу отгрузок владельца (решение 2026-10-21).

Строки — в порядке TOP.xlsx с заголовками подгрупп, как на экране «Остатки».
Колонки (буквы — как в гугл-таблице, чтобы вставка легла на свои места):

    ДО отгрузок:   C Учительская, D Коминтерна (остаток до отгрузок дня),
                   F задачи Учительской, G задачи Коминтерна,
                   I OZON утро (Озон1), J КГТ, K СДЭК, L WB, M СГТ, N Ozon 2, O Ozon 3
                   — отгружено за день с площадки отгрузки по каналам и машинам;
    ПОСЛЕ отгрузок: S склад − отгрузка, T склад + задачи − отгрузка (площадка отгрузки),
                   U задачи Коминтерна, V задачи Учительской,
                   X Учительская + задачи, Z общее (T + X).

«До отгрузок» = остаток сейчас + отгружено за день: так S = D − отгрузка совпадает с
остатком после погрузки, как в таблице.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, Movement, Pallet, Site, Unit
from app.seed import RETURNS_SITE_NAME
from app.services.stock import active_sites, stock_by_article_site
from app.services.tasks import plan_sum_by_site
from app.statuses import MV_SHIP, MV_SHIP_CANCEL, OZON, OZON_TRUCKS
from app.timeutil import fmt_dt, msk_day_bounds, now_utc

# Колонки каналов в порядке гугл-таблицы: (заголовок, канал, машина Ozon или None)
CHANNEL_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("OZON утро", OZON, OZON_TRUCKS[0]),
    ("КГТ", "КГТ", None),
    ("СДЭК", "СДЭК", None),
    ("WB", "WB", None),
    ("СГТ", "СГТ", None),
    ("Ozon 2", OZON, OZON_TRUCKS[1]),
    ("Ozon 3", OZON, OZON_TRUCKS[2]),
)

# Площадка, с которой идут отгрузки (колонки «ком» в таблице)
SHIP_SITE_NAME = RETURNS_SITE_NAME
OTHER_SITE_NAME = "Учительская"


def _column_index(channel: str, note: str | None) -> int:
    """Номер колонки канала. Ozon без выбранной машины (канал ещё открыт) — в «OZON утро»."""
    for index, (_, col_channel, truck) in enumerate(CHANNEL_COLUMNS):
        if col_channel != channel:
            continue
        if truck is None or note == truck:
            return index
    return 0 if channel == OZON else -1


@dataclass
class ShipmentRow:
    """Строка отгрузки: артикул или заголовок подгруппы."""

    name: str
    is_header: bool
    color_hex: str | None
    before_other: int = 0  # Учительская, до отгрузок
    before_ship: int = 0  # Коминтерна, до отгрузок
    tasks_other: int = 0
    tasks_ship: int = 0
    channels: list[int] = field(default_factory=list)

    @property
    def shipped(self) -> int:
        return sum(self.channels)

    @property
    def after(self) -> int:
        """Склад − отгрузка (площадка отгрузки)."""
        return self.before_ship - self.shipped

    @property
    def after_with_tasks(self) -> int:
        """Склад + задачи − отгрузка."""
        return self.before_ship + self.tasks_ship - self.shipped

    @property
    def other_with_tasks(self) -> int:
        """Учительская + задачи."""
        return self.before_other + self.tasks_other

    @property
    def total(self) -> int:
        """Общее: (склад + задачи − отгрузка) + (Учительская + задачи)."""
        return self.after_with_tasks + self.other_with_tasks


@dataclass
class ShipmentTable:
    """Таблица отгрузки за день."""

    day: date
    ship_site: Site | None
    other_site: Site | None
    rows: list[ShipmentRow]
    built_at: str
    totals: list[int] = field(default_factory=list)  # отгружено по каналам за день

    def tsv_before(self) -> str:
        """C…O гугл-таблицы: пустые колонки E и H оставлены, чтобы вставка легла на место."""
        lines = []
        for row in self.rows:
            if row.is_header:
                lines.append("")
                continue
            values = [
                _num(row.before_other),
                _num(row.before_ship),
                "",
                _blank_zero(row.tasks_other),
                _blank_zero(row.tasks_ship),
                "",
                *[_blank_zero(value) for value in row.channels],
            ]
            lines.append("\t".join(values))
        return "\n".join(lines)

    def tsv_after(self) -> str:
        """S…Z гугл-таблицы: пустые колонки W и Y оставлены."""
        lines = []
        for row in self.rows:
            if row.is_header:
                lines.append("")
                continue
            values = [
                _num(row.after),
                _num(row.after_with_tasks),
                _blank_zero(row.tasks_ship),
                _blank_zero(row.tasks_other),
                "",
                _num(row.other_with_tasks),
                "",
                _num(row.total),
            ]
            lines.append("\t".join(values))
        return "\n".join(lines)


def _num(value: int) -> str:
    return str(value)


def _blank_zero(value: int) -> str:
    """В таблице пустые ячейки вместо нулей у задач и отгрузок."""
    return "" if value == 0 else str(value)


def shipped_by_article_column(
    session: Session, *, day: date, site_id: int
) -> dict[tuple[int, int], int]:
    """Отгружено за день с площадки: (артикул, колонка канала) → штук.

    Считаются движения «Отгрузка» за московские сутки, кроме отменённых позже.
    """
    start, end = msk_day_bounds(day)
    ships = session.execute(
        select(Movement.unit_id, Movement.created_at, Unit.article_id, Pallet.channel, Pallet.note)
        .join(Unit, Unit.id == Movement.unit_id)
        .join(Pallet, Pallet.id == Movement.ref_id)
        .where(
            Movement.type == MV_SHIP,
            Movement.created_at >= start,
            Movement.created_at < end,
            Movement.from_site_id == site_id,
        )
    ).all()
    if not ships:
        return {}
    cancels: dict[int, list] = defaultdict(list)
    for unit_id, created_at in session.execute(
        select(Movement.unit_id, Movement.created_at).where(
            Movement.type == MV_SHIP_CANCEL,
            Movement.unit_id.in_({row[0] for row in ships}),
        )
    ).all():
        cancels[int(unit_id)].append(created_at)

    result: dict[tuple[int, int], int] = defaultdict(int)
    for unit_id, created_at, article_id, channel, note in ships:
        if any(moment >= created_at for moment in cancels.get(int(unit_id), [])):
            continue
        column = _column_index(str(channel), note)
        if column >= 0:
            result[(int(article_id), column)] += 1
    return dict(result)


def build_shipment_table(session: Session, *, day: date) -> ShipmentTable:
    """Собрать таблицу отгрузки за день в порядке строк TOP.xlsx."""
    sites = active_sites(session)
    ship_site = next((s for s in sites if s.name == SHIP_SITE_NAME), sites[0] if sites else None)
    other_site = next((s for s in sites if ship_site is None or s.id != ship_site.id), None)

    stock = stock_by_article_site(session)
    plans = plan_sum_by_site(session, day=day)
    shipped: dict[tuple[int, int], int] = {}
    shipped_other: dict[tuple[int, int], int] = {}
    if ship_site is not None:
        shipped = shipped_by_article_column(session, day=day, site_id=ship_site.id)
    if other_site is not None:
        shipped_other = shipped_by_article_column(session, day=day, site_id=other_site.id)

    articles = list(session.scalars(select(Article).order_by(Article.sort_order, Article.id)).all())
    rows: list[ShipmentRow] = []
    totals = [0 for _ in CHANNEL_COLUMNS]
    for article in articles:
        if article.kind == "header":
            rows.append(ShipmentRow(name=article.name, is_header=True, color_hex=article.color_hex))
            continue
        channels = [shipped.get((article.id, index), 0) for index in range(len(CHANNEL_COLUMNS))]
        for index, value in enumerate(channels):
            totals[index] += value
        other_shipped = sum(
            shipped_other.get((article.id, index), 0) for index in range(len(CHANNEL_COLUMNS))
        )
        rows.append(
            ShipmentRow(
                name=article.name,
                is_header=False,
                color_hex=article.color_hex,
                before_other=(stock.get((article.id, other_site.id), 0) + other_shipped)
                if other_site
                else 0,
                before_ship=(stock.get((article.id, ship_site.id), 0) + sum(channels))
                if ship_site
                else 0,
                tasks_other=plans.get((article.id, other_site.id), 0) if other_site else 0,
                tasks_ship=plans.get((article.id, ship_site.id), 0) if ship_site else 0,
                channels=channels,
            )
        )
    return ShipmentTable(
        day=day,
        ship_site=ship_site,
        other_site=other_site,
        rows=rows,
        built_at=fmt_dt(now_utc()),
        totals=totals,
    )
