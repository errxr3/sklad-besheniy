"""Экран «Остатки» — повторяет левый блок гугл-таблицы «ДО отгрузок» (SPEC §6.9)."""

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Movement, Site, Unit
from app.services.tasks import plan_sum_by_site
from app.statuses import IN_STOCK
from app.timeutil import now_utc


@dataclass
class StockRow:
    """Строка таблицы остатков: артикул или заголовок подгруппы."""

    article_id: int | None
    name: str
    is_header: bool
    color_hex: str | None
    active: bool
    stock: list[int] = field(default_factory=list)  # по площадкам, в порядке колонок
    tasks: list[int | None] = field(default_factory=list)  # пусто, если задач нет

    def stock_text(self, index: int) -> str:
        """Значение остатка для вывода (у заголовка — пусто)."""
        if self.is_header:
            return ""
        return str(self.stock[index])

    def tasks_text(self, index: int) -> str:
        """Значение задач: пусто, если задач нет (как в таблице, не 0)."""
        if self.is_header:
            return ""
        value = self.tasks[index]
        return "" if value in (None, 0) else str(value)


@dataclass
class StockTable:
    """Таблица остатков целиком."""

    sites: list[Site]
    rows: list[StockRow]
    day: date
    built_at: str

    @property
    def row_count(self) -> int:
        """Число строк (вместе с заголовками подгрупп)."""
        return len(self.rows)

    def column_values(self, kind: str, site_index: int) -> list[str]:
        """Значения столбца сверху вниз, включая пустые на строках-заголовках.

        Нужно для кнопок «Копировать столбец»: вставка в гугл-таблицу должна лечь
        ровно напротив своих артикулов (SPEC §6.9).
        """
        if kind == "stock":
            return [row.stock_text(site_index) for row in self.rows]
        return [row.tasks_text(site_index) for row in self.rows]


def site_locative(name: str) -> str:
    """Название площадки в форме «Задачи на …» (как в гугл-таблице).

    «Учительская» → «Учительской», остальные названия остаются как есть.
    """
    if name.endswith("ская"):
        return name[:-2] + "ой"
    if name.endswith("ский"):
        return name[:-2] + "ом"
    return name


def stock_by_article_site(session: Session) -> dict[tuple[int, int], int]:
    """Остатки: количество зеркал «На складе» по (артикул, площадка)."""
    rows = session.execute(
        select(Unit.article_id, Unit.site_id, func.count())
        .where(Unit.status == IN_STOCK)
        .group_by(Unit.article_id, Unit.site_id)
    ).all()
    return {(int(article_id), int(site_id)): int(count) for article_id, site_id, count in rows}


def stock_at(session: Session, moment: datetime) -> dict[tuple[int, int], int]:
    """Остатки на момент времени, восстановленные по журналу движений.

    Журнал неизменяем (SPEC §8), поэтому состояние любого прошедшего дня считается точно:
    для каждого зеркала берётся последнее движение до момента — его статус и площадка.
    """
    last = (
        select(Movement.unit_id, Movement.to_status, Movement.to_site_id)
        .where(Movement.created_at < moment)
        .distinct(Movement.unit_id)
        .order_by(Movement.unit_id, Movement.created_at.desc(), Movement.id.desc())
        .subquery()
    )
    rows = session.execute(
        select(Unit.article_id, last.c.to_site_id, func.count())
        .join(last, last.c.unit_id == Unit.id)
        .where(last.c.to_status == IN_STOCK)
        .group_by(Unit.article_id, last.c.to_site_id)
    ).all()
    return {
        (int(article_id), int(site_id)): int(count)
        for article_id, site_id, count in rows
        if site_id is not None
    }


def active_sites(session: Session) -> list[Site]:
    """Площадки в порядке, заданном администратором (SPEC §6.9)."""
    return list(
        session.scalars(
            select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id)
        ).all()
    )


def build_stock_table(
    session: Session, *, day: date | None = None, hide_inactive: bool = False
) -> StockTable:
    """Собрать таблицу остатков в порядке строк TOP.xlsx.

    Строки идут ровно как в файле: сначала «Полотна», затем «Алюминий», затем
    «Безрамные», внутри листа — сверху вниз. Заголовки подгрупп остаются строками.
    Неактивные артикулы по умолчанию не прячутся — иначе при вставке в гугл-таблицу
    съедет порядок.
    """
    from app.timeutil import fmt_dt, today_msk

    target_day = day or today_msk()
    sites = active_sites(session)
    stock = stock_by_article_site(session)
    plans = plan_sum_by_site(session, day=target_day)

    query = select(Article).order_by(Article.sort_order, Article.id)
    if hide_inactive:
        query = query.where((Article.kind == "header") | (Article.active.is_(True)))
    articles = list(session.scalars(query).all())

    rows: list[StockRow] = []
    for article in articles:
        if article.kind == "header":
            rows.append(
                StockRow(
                    article_id=None,
                    name=article.name,
                    is_header=True,
                    color_hex=article.color_hex,
                    active=True,
                    stock=[0 for _ in sites],
                    tasks=[None for _ in sites],
                )
            )
            continue
        rows.append(
            StockRow(
                article_id=article.id,
                name=article.name,
                is_header=False,
                color_hex=article.color_hex,
                active=article.active,
                stock=[stock.get((article.id, site.id), 0) for site in sites],
                tasks=[plans.get((article.id, site.id)) for site in sites],
            )
        )

    return StockTable(sites=sites, rows=rows, day=target_day, built_at=fmt_dt(now_utc()))


def table_to_tsv(table: StockTable, columns: list[tuple[str, int]]) -> str:
    """Столбцы таблицы в TSV: одно значение на строку, пустые строки на заголовках."""
    lines = []
    for index in range(table.row_count):
        values = [table.column_values(kind, site_index)[index] for kind, site_index in columns]
        lines.append("\t".join(values))
    return "\n".join(lines)


def sheet_rows(table: StockTable) -> list[list[str]]:
    """Строки для листа «Остатки из системы» (SPEC §10).

    A: артикул (как в TOP) | B..: остатки по площадкам | затем задачи по площадкам.
    """
    result: list[list[str]] = []
    for row in table.rows:
        line = [row.name]
        line.extend(row.stock_text(index) for index in range(len(table.sites)))
        line.extend(row.tasks_text(index) for index in range(len(table.sites)))
        result.append(line)
    return result
