"""Экран «Отгрузка» — повторяет гугл-таблицу отгрузок владельца (решение 2026-10-21).

Строки — в порядке TOP.xlsx с заголовками подгрупп, как на экране «Остатки».
Колонки (буквы — как в гугл-таблице, чтобы вставка легла на свои места):

    ДО отгрузок:   C Учительская, D Коминтерна (остаток до отгрузок дня),
                   F задачи Учительской, G задачи Коминтерна,
                   I Озон 1, J КГТ, K СДЭК, L WB, M СГТ, N Озон 2, O Озон 3
                   — отгружено за день с площадки отгрузки по ЗАКРЫТЫМ каналам и машинам;
                   повторные закрытия одного канала за день суммируются по артикулам;
    ПОСЛЕ отгрузок: S склад − отгрузка, T склад + задачи − отгрузка — склад общий (обе
                   площадки), задачи обеих площадок, только несделанная часть;
                   U задачи Коминтерна, V задачи Учительской.

«До отгрузок» = остаток (сейчас или на конец прошедшего дня — по журналу движений) + всё
отгруженное за день, в том числе в ещё открытом канале.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Movement, Pallet, Site, Task, Unit, User
from app.seed import RETURNS_SITE_NAME
from app.services.shipping import AUTO_CLOSE_NOTE, open_channels
from app.services.stock import active_sites, stock_at, stock_by_article_site
from app.services.tasks import plan_sum_by_site
from app.statuses import (
    MV_INVENTORY_FIX,
    MV_LABEL_VOID,
    MV_SHIP,
    MV_SHIP_CANCEL,
    MV_WRITE_OFF,
    OZON,
    OZON_TRUCKS,
    WRITTEN_OFF,
)
from app.timeutil import fmt_dt, msk_day_bounds, now_utc, today_msk

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
    """Номер колонки канала (−1 — колонки нет).

    Ozon без выбранной машины (канал закрыт автоматически в конце дня) — в «Озон 1».
    """
    for index, (_, col_channel, truck) in enumerate(CHANNEL_COLUMNS):
        if col_channel != channel:
            continue
        # «Озон2» или «Озон2, закрыт автоматически»
        if truck is None or note == truck or (note or "").startswith(truck + ","):
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
    tasks_ship_done: int = 0  # из задач дня уже сделано и принято сканом
    tasks_other_done: int = 0
    tasks_ship_who: str = ""  # кто делает: «Иван — 5, Пётр — 3»
    tasks_other_who: str = ""
    channels: list[int] = field(default_factory=list)
    # Отсканировано в ещё открытые каналы — показывается серым «+N», в итог не входит
    pending: list[int] = field(default_factory=list)

    @property
    def shipped(self) -> int:
        return sum(self.channels)

    @property
    def stock_total(self) -> int:
        """Склад общий: обе площадки вместе (решение владельца 2026-10-21)."""
        return self.before_ship + self.before_other

    @property
    def after(self) -> int:
        """Склад − отгрузка (склад общий)."""
        return self.stock_total - self.shipped

    @property
    def tasks_left(self) -> int:
        """Сколько по задачам обеих площадок ещё не сделано (сделанное уже на складе)."""
        return max(0, self.tasks_ship - self.tasks_ship_done) + max(
            0, self.tasks_other - self.tasks_other_done
        )

    @property
    def after_with_tasks(self) -> int:
        """Склад + задачи − отгрузка (склад общий, задачи обеих площадок).

        Задачи прибавляются сразу, как начальник смены их поставил, но только несделанная
        часть: готовое зеркало после скана уже в складе, иначе оно посчиталось бы дважды.
        """
        return self.stock_total + self.tasks_left - self.shipped


@dataclass
class ShipmentTable:
    """Таблица отгрузки за день."""

    day: date
    ship_site: Site | None
    other_site: Site | None
    rows: list[ShipmentRow]
    built_at: str
    is_today: bool = True
    open_channels: list[Pallet] = field(default_factory=list)  # не закрыты — не в таблице
    auto_closed: list[Pallet] = field(default_factory=list)  # закрыты автоматически в этот день
    totals: list[int] = field(default_factory=list)  # отгружено по каналам за день
    pending_totals: list[int] = field(default_factory=list)  # в открытых каналах, серым


def shipped_by_article_column(
    session: Session,
    *,
    day: date,
    site_id: int,
    closed_only: bool = True,
    pending: dict[tuple[int, int], int] | None = None,
) -> dict[tuple[int, int], int]:
    """Отгружено за день с площадки: (артикул, колонка канала) → штук.

    Считаются движения «Отгрузка» за московские сутки, кроме отменённых позже.
    По умолчанию — только из закрытых каналов: грузчик нажал «Закрыть канал».
    С `closed_only=False` открытые каналы идут в колонку −1 (нужно для «до отгрузок»).
    В `pending` (если передан) копится отсканированное в открытые каналы по их колонкам.
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
            if pending is not None:
                pending[(int(article_id), _column_index(str(channel), note))] += 1
            if not closed_only:
                result[(int(article_id), -1)] += 1
            continue
        column = _column_index(str(channel), note)
        if column >= 0 or not closed_only:
            result[(int(article_id), column)] += 1
    return dict(result)


def tasks_done_by_article_site(session: Session, *, day: date) -> dict[tuple[int, int], int]:
    """Из задач на день уже сделано: этикетки задач, принятые до конца дня.

    Не считаются этикетки, убранные потом как лишние (сдали «Собрал всё», а зеркало не
    сделали) или списанные — как в отчёте «план/факт».
    """
    end = msk_day_bounds(day)[1]
    removed = (
        select(Movement.id)
        .where(
            Movement.unit_id == Unit.id,
            Movement.to_status == WRITTEN_OFF,
            Movement.type.in_((MV_WRITE_OFF, MV_LABEL_VOID, MV_INVENTORY_FIX)),
            Movement.created_at < end,
        )
        .exists()
    )
    rows = session.execute(
        select(Task.article_id, Task.site_id, func.count())
        .join(Unit, Unit.task_id == Task.id)
        .where(
            Task.date == day,
            Task.kind == "article",
            Unit.accepted_at.is_not(None),
            Unit.accepted_at < end,
            ~removed,
        )
        .group_by(Task.article_id, Task.site_id)
    ).all()
    return {
        (int(article_id), int(site_id)): int(count)
        for article_id, site_id, count in rows
        if article_id is not None
    }


def task_workers(session: Session, *, day: date) -> dict[tuple[int, int], str]:
    """Кто делает задачи дня: (артикул, площадка) → «Иван — 5, Пётр — 3»."""
    rows = session.execute(
        select(Task.article_id, Task.site_id, User.name, Task.qty_plan)
        .join(User, User.id == Task.user_id)
        .where(Task.date == day, Task.kind == "article")
        .order_by(User.name)
    ).all()
    people: dict[tuple[int, int], list[str]] = defaultdict(list)
    for article_id, site_id, name, qty in rows:
        if article_id is not None:
            people[(int(article_id), int(site_id))].append(f"{name} — {qty}")
    return {key: ", ".join(value) for key, value in people.items()}


def build_shipment_table(session: Session, *, day: date) -> ShipmentTable:
    """Собрать таблицу отгрузки за день в порядке строк TOP.xlsx."""
    sites = active_sites(session)
    ship_site = next((s for s in sites if s.name == SHIP_SITE_NAME), sites[0] if sites else None)
    other_site = next((s for s in sites if ship_site is None or s.id != ship_site.id), None)

    # Сегодня — остаток сейчас; прошедший день — остаток на конец того дня по журналу
    if day >= today_msk():
        stock = stock_by_article_site(session)
    else:
        stock = stock_at(session, msk_day_bounds(day)[1])
    plans = plan_sum_by_site(session, day=day)
    done = tasks_done_by_article_site(session, day=day)
    workers = task_workers(session, day=day)
    shipped: dict[tuple[int, int], int] = {}
    shipped_other: dict[tuple[int, int], int] = {}
    pending: dict[tuple[int, int], int] = defaultdict(int)
    if ship_site is not None:
        shipped = shipped_by_article_column(
            session, day=day, site_id=ship_site.id, closed_only=False, pending=pending
        )
    if other_site is not None:
        shipped_other = shipped_by_article_column(
            session, day=day, site_id=other_site.id, closed_only=False, pending=pending
        )

    articles = list(session.scalars(select(Article).order_by(Article.sort_order, Article.id)).all())
    rows: list[ShipmentRow] = []
    totals = [0 for _ in CHANNEL_COLUMNS]
    pending_totals = [0 for _ in CHANNEL_COLUMNS]
    for article in articles:
        if article.kind == "header":
            rows.append(ShipmentRow(name=article.name, is_header=True, color_hex=article.color_hex))
            continue
        # Отгрузки со всех площадок: склад общий
        channels = [
            shipped.get((article.id, index), 0) + shipped_other.get((article.id, index), 0)
            for index in range(len(CHANNEL_COLUMNS))
        ]
        for index, value in enumerate(channels):
            totals[index] += value
            pending_totals[index] += pending.get((article.id, index), 0)
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
                tasks_ship_done=done.get((article.id, ship_site.id), 0) if ship_site else 0,
                tasks_other_done=done.get((article.id, other_site.id), 0) if other_site else 0,
                tasks_ship_who=workers.get((article.id, ship_site.id), "") if ship_site else "",
                tasks_other_who=workers.get((article.id, other_site.id), "") if other_site else "",
                channels=channels,
                pending=[pending.get((article.id, index), 0) for index in range(len(CHANNEL_COLUMNS))],
            )
        )
    return ShipmentTable(
        day=day,
        ship_site=ship_site,
        other_site=other_site,
        rows=rows,
        built_at=fmt_dt(now_utc()),
        totals=totals,
        pending_totals=pending_totals,
        is_today=day >= today_msk(),
        open_channels=open_channels(session),
        auto_closed=list(
            session.scalars(
                select(Pallet).where(
                    Pallet.note.like(f"%{AUTO_CLOSE_NOTE}"),
                    Pallet.closed_at >= msk_day_bounds(day)[0],
                    Pallet.closed_at < msk_day_bounds(day)[1],
                )
            ).all()
        ),
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
        row_index += 1

    for letter in ("B", "R"):
        sheet.column_dimensions[letter].width = 26
    for letter in ("A", "E", "H", "P", "Q"):
        sheet.column_dimensions[letter].width = 3
    for letter in ("C", "D", "F", "G", "S", "T", "U", "V", *channel_letters):
        sheet.column_dimensions[letter].width = 11
    sheet.freeze_panes = "C4"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
