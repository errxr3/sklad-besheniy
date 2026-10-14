"""Журнал движений и отчёт «Выработка» (SPEC §6.10)."""

import csv
import io
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload

from app.models import Article, Movement, Unit, User
from app.statuses import (
    MOVEMENT_LABELS,
    MV_ACCEPT,
    MV_INVENTORY_FIX,
    MV_LABEL_VOID,
    MV_WRITE_OFF,
    PRINTED,
    ROLE_ASSEMBLER,
    STATUS_LABELS,
    WRITTEN_OFF,
)
from app.timeutil import fmt_dt, msk_day_bounds

PAGE_SIZE = 50


@dataclass
class JournalFilters:
    """Фильтры журнала (SPEC §6.10)."""

    date_from: date | None = None
    date_to: date | None = None
    article_id: int | None = None
    user_id: int | None = None
    movement_type: str | None = None
    site_id: int | None = None
    number: str | None = None


def _apply_filters(query: Select, filters: JournalFilters) -> Select:
    """Наложить фильтры на запрос журнала."""
    if filters.date_from:
        start, _ = msk_day_bounds(filters.date_from)
        query = query.where(Movement.created_at >= start)
    if filters.date_to:
        _, end = msk_day_bounds(filters.date_to)
        query = query.where(Movement.created_at < end)
    if filters.user_id:
        query = query.where(Movement.user_id == filters.user_id)
    if filters.movement_type:
        query = query.where(Movement.type == filters.movement_type)
    if filters.site_id:
        query = query.where(
            (Movement.to_site_id == filters.site_id) | (Movement.from_site_id == filters.site_id)
        )
    if filters.article_id or filters.number:
        query = query.join(Unit, Unit.id == Movement.unit_id)
        if filters.article_id:
            query = query.where(Unit.article_id == filters.article_id)
        if filters.number:
            query = query.where(Unit.number == filters.number.strip().upper())
    return query


def journal_page(
    session: Session, filters: JournalFilters, page: int = 1, page_size: int = PAGE_SIZE
) -> tuple[list[Movement], int]:
    """Страница журнала и общее число записей."""
    base = select(Movement)
    total_query = _apply_filters(select(func.count()).select_from(Movement), filters)
    total = int(session.scalar(total_query) or 0)

    query = _apply_filters(base, filters)
    query = (
        query.options(joinedload(Movement.unit).joinedload(Unit.article), joinedload(Movement.user))
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .limit(page_size)
        .offset(max(0, (page - 1) * page_size))
    )
    rows = list(session.scalars(query).unique().all())
    return rows, total


def journal_csv(session: Session, filters: JournalFilters, limit: int = 20000) -> bytes:
    """Журнал в CSV для Excel (UTF-8 с BOM, разделитель «;»)."""
    query = _apply_filters(select(Movement), filters)
    query = (
        query.options(joinedload(Movement.unit).joinedload(Unit.article), joinedload(Movement.user))
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .limit(limit)
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Когда", "Номер", "Артикул", "Операция", "Было", "Стало", "Кто", "Примечание"])
    for movement in session.scalars(query).unique().all():
        writer.writerow(
            [
                fmt_dt(movement.created_at),
                movement.unit.number if movement.unit else "",
                movement.unit.article.name if movement.unit and movement.unit.article else "",
                MOVEMENT_LABELS.get(movement.type, movement.type),
                STATUS_LABELS.get(movement.from_status or "", ""),
                STATUS_LABELS.get(movement.to_status, movement.to_status),
                movement.user.name if movement.user else "",
                movement.note or "",
            ]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


@dataclass
class OutputRow:
    """Строка отчёта «Выработка»."""

    assembler: str
    article: str
    made: int
    scrapped: int

    @property
    def defect_percent(self) -> float:
        """Доля брака, %."""
        return round(self.scrapped * 100 / self.made, 1) if self.made else 0.0


def output_report(
    session: Session, *, date_from: date, date_to: date, site_id: int | None = None
) -> list[OutputRow]:
    """«Выработка»: сделано по артикулам, из них списано в брак (SPEC §6.10)."""
    start, _ = msk_day_bounds(date_from)
    _, end = msk_day_bounds(date_to)

    made_query = (
        select(User.name, Article.name, func.count(), Unit.id)
        .join(Unit, Unit.assembler_id == User.id)
        .join(Article, Article.id == Unit.article_id)
        .join(Movement, Movement.unit_id == Unit.id)
        .where(
            Movement.type == MV_ACCEPT,
            Movement.from_status == PRINTED,
            Movement.created_at >= start,
            Movement.created_at < end,
            User.role == ROLE_ASSEMBLER,
        )
    )
    if site_id:
        made_query = made_query.where(Unit.site_id == site_id)

    made: dict[tuple[str, str], set[int]] = {}
    for user_name, article_name, _count, unit_id in session.execute(
        made_query.group_by(User.name, Article.name, Unit.id)
    ).all():
        made.setdefault((str(user_name), str(article_name)), set()).add(int(unit_id))

    scrapped_ids = set(
        session.scalars(
            select(Movement.unit_id).where(
                Movement.to_status == WRITTEN_OFF,
                Movement.type.in_((MV_WRITE_OFF, MV_LABEL_VOID, MV_INVENTORY_FIX)),
                Movement.created_at >= start,
                Movement.created_at < end,
            )
        ).all()
    )

    rows = [
        OutputRow(
            assembler=assembler,
            article=article,
            made=len(unit_ids),
            scrapped=len(unit_ids & scrapped_ids),
        )
        for (assembler, article), unit_ids in sorted(made.items())
    ]
    return rows
