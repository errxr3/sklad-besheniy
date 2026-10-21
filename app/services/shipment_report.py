"""Экран «Отгрузка» — повторяет гугл-таблицу отгрузок владельца (решение 2026-10-21).

Строки — в порядке TOP.xlsx с заголовками подгрупп, как на экране «Остатки».
Колонки (буквы — как в гугл-таблице, чтобы вставка легла на свои места):

    ДО отгрузок:   C Учительская, D Коминтерна (остаток до отгрузок дня),
                   F задачи Учительской, G задачи Коминтерна,
                   I Озон 1, J КГТ, K СДЭК, L WB, M СГТ, N Озон 2, O Озон 3
                   — отгружено за день с площадки отгрузки по ЗАКРЫТЫМ каналам и машинам;
                   повторные закрытия одного канала за день суммируются по артикулам;
    ПОСЛЕ отгрузок: S склад − отгрузка, T склад + задачи − отгрузка (площадка отгрузки),
                   U задачи Коминтерна, V задачи Учительской,
                   X Учительская + задачи, Z общее (T + X).

«До отгрузок» = остаток сейчас + всё отгруженное за день (в том числе в ещё открытом канале):
пока канал открыт, его зеркала уже не на складе, но в колонки ещё не попали.
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
    ("Озон 1", OZON, OZON_TRUCKS[0]),
    ("КГТ", "КГТ", None),
    ("СДЭК", "СДЭК", None),
    ("WB", "WB", None),
    ("СГТ", "СГТ", None),
    ("Озон 2", OZON, OZON_TRUCKS[1]),
    ("Озон 3", OZON, OZON_TRUCKS[2]),
)

# Площадка, с которой идут отгрузки (колонки «ком» в таблице)
SHIP_SITE_NAME = RETURNS_SITE_NAME
OTHER_SITE_NAME = "Учительская"


def _column_index(channel: str, note: str | None) -> int:
    """Номер колонки канала (−1 — колонки нет)."""
    for index, (_, col_channel, truck) in enumerate(CHANNEL_COLUMNS):
        if col_channel != channel:
            continue
        if truck is None or note == truck:
            return index
    return -1


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


def shipped_by_article_column(
    session: Session, *, day: date, site_id: int, closed_only: bool = True
) -> dict[tuple[int, int], int]:
    """Отгружено за день с площадки: (артикул, колонка канала) → штук.

    Считаются движения «Отгрузка» за московские сутки, кроме отменённых позже.
    По умолчанию — только из закрытых каналов: грузчик нажал «Закрыть канал».
    С `closed_only=False` открытые каналы идут в колонку −1 (нужно для «до отгрузок»).
    """
    start, end = msk_day_bounds(day)
    ships = session.execute(
        select(
            Movement.unit_id,
            Movement.created_at,
            Unit.article_id,
            Pallet.channel,
            Pallet.note,
            Pallet.closed_at,
        )
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
    for unit_id, created_at, article_id, channel, note, closed_at in ships:
        if any(moment >= created_at for moment in cancels.get(int(unit_id), [])):
            continue
        if closed_at is None:
            if not closed_only:
                result[(int(article_id), -1)] += 1
            continue
        column = _column_index(str(channel), note)
        if column >= 0 or not closed_only:
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
        shipped = shipped_by_article_column(session, day=day, site_id=ship_site.id, closed_only=False)
    if other_site is not None:
        shipped_other = shipped_by_article_column(
            session, day=day, site_id=other_site.id, closed_only=False
        )

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
            count for (article_id, _), count in shipped_other.items() if article_id == article.id
        )
        # Всё отгруженное за день, включая открытые каналы — для «до отгрузок»
        all_shipped = sum(
            count for (article_id, _), count in shipped.items() if article_id == article.id
        )
        rows.append(
            ShipmentRow(
                name=article.name,
                is_header=False,
                color_hex=article.color_hex,
                before_other=(stock.get((article.id, other_site.id), 0) + other_shipped)
                if other_site
                else 0,
                before_ship=(stock.get((article.id, ship_site.id), 0) + all_shipped)
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


def shipment_xlsx(table: ShipmentTable) -> bytes:
    """Полный файл отгрузки — те же столбцы, что в гугл-таблице владельца.

    B артикул | C Учительская | D Коминтерна | F, G задачи | I…O каналы ||
    R артикул | S склад − отгрузка | T склад + задачи − отгрузка | U, V задачи |
    X Учительская + задачи | Z общее. Строка 1 — блоки, 2 — заголовки, 3 — дата.
    """
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    other = table.other_site.name if table.other_site else ""
    ship = table.ship_site.name if table.ship_site else ""
    channel_letters = ["I", "J", "K", "L", "M", "N", "O"]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = f"Отгрузка {table.day:%d.%m}"

    thin = Side(style="thin", color="B7BCD6")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    fills = {
        "before": PatternFill("solid", fgColor="FFB86C"),
        "after": PatternFill("solid", fgColor="7BE38F"),
        "head": PatternFill("solid", fgColor="EDEBFF"),
        "channel": PatternFill("solid", fgColor="FDE6D2"),
        "channel_cell": PatternFill("solid", fgColor="FFF4D6"),
        "zero": PatternFill("solid", fgColor="F8C9CD"),
        "neg": PatternFill("solid", fgColor="E8463C"),
    }

    sheet["B1"] = "ДО отгрузок"
    sheet["B1"].fill = fills["before"]
    sheet["R1"] = "ПОСЛЕ отгрузок"
    sheet["R1"].fill = fills["after"]
    sheet["S1"] = ship[:3].lower()
    sheet["T1"] = ship[:3].lower()
    for ref in ("B1", "R1", "S1", "T1"):
        sheet[ref].font = bold

    headers = {
        "B": "Артикул", "C": other, "D": ship,
        "F": f"Задачи на {other}", "G": f"Задачи на {ship}",
        "R": "Артикул", "S": "склад - отгрузка", "T": "СКЛАД + задачи - отгрузки",
        "U": f"Задачи на {ship}", "V": f"Задачи на {other}",
        "X": f"{other} + задачи", "Z": "общее",
    }
    for letter, (title, _, _) in zip(channel_letters, CHANNEL_COLUMNS, strict=True):
        headers[letter] = title
    for letter, title in headers.items():
        cell = sheet[f"{letter}2"]
        cell.value = title
        cell.font = bold
        cell.alignment = center
        cell.border = border
        cell.fill = fills["channel"] if letter in channel_letters else fills["head"]
    for letter in ("C", "F", "U"):
        cell = sheet[f"{letter}3"]
        cell.value = f"{table.day:%d.%m}"
        cell.alignment = center
    sheet.row_dimensions[2].height = 48

    def put(row_index: int, letter: str, value: int | str, *, blank_zero: bool = False) -> None:
        cell = sheet[f"{letter}{row_index}"]
        cell.value = "" if (blank_zero and value == 0) else value
        cell.border = border
        if letter in channel_letters:
            cell.fill = fills["channel_cell"]
        if isinstance(value, int) and not blank_zero:
            if value < 0:
                cell.fill = fills["neg"]
                cell.font = Font(bold=True, color="FFFFFF")
            elif value == 0:
                cell.fill = fills["zero"]

    row_index = 4
    for row in table.rows:
        if row.is_header:
            for letter in ("B", "R"):
                cell = sheet[f"{letter}{row_index}"]
                cell.value = row.name
                cell.font = bold
            row_index += 1
            continue
        name_fill = PatternFill("solid", fgColor=row.color_hex.lstrip("#")) if row.color_hex else None
        for letter in ("B", "R"):
            cell = sheet[f"{letter}{row_index}"]
            cell.value = row.name
            cell.border = border
            cell.font = bold
            if name_fill:
                cell.fill = name_fill
        put(row_index, "C", row.before_other)
        put(row_index, "D", row.before_ship)
        put(row_index, "F", row.tasks_other, blank_zero=True)
        put(row_index, "G", row.tasks_ship, blank_zero=True)
        for letter, value in zip(channel_letters, row.channels, strict=True):
            put(row_index, letter, value, blank_zero=True)
        put(row_index, "S", row.after)
        put(row_index, "T", row.after_with_tasks)
        put(row_index, "U", row.tasks_ship, blank_zero=True)
        put(row_index, "V", row.tasks_other, blank_zero=True)
        put(row_index, "X", row.other_with_tasks)
        put(row_index, "Z", row.total)
        row_index += 1

    for letter in ("B", "R"):
        sheet.column_dimensions[letter].width = 26
    for letter in ("A", "E", "H", "P", "Q", "W", "Y"):
        sheet.column_dimensions[letter].width = 3
    for letter in ("C", "D", "F", "G", "S", "T", "U", "V", "X", "Z", *channel_letters):
        sheet.column_dimensions[letter].width = 11
    sheet.freeze_panes = "C4"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
