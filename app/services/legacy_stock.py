"""Старый остаток без этикеток (решение владельца 2026-11-03).

Обклеивать весь склад на запуске долго (около 2500 зеркал). Поэтому остаток вносится числом
из Excel: «сколько каждого артикула стоит на полке». Склад заводит на них обычные зеркала со
своими номерами, но этикетку не печатает (`unlabeled`). Такое зеркало грузится по наклейке
маркетплейса: грузчик пикает наклейку Ozon/WB и жмёт «Старое зеркало, без этикетки» — склад
отгружает одно старое зеркало этого артикула. Без партии (СДЭК) артикул выбирают из списка.

В Excel — всё, что стоит на полке. Что уже есть на складе с этикетками (сборщики сдали после
запуска), склад вычитает сам, чтобы не задвоить.
"""

from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Site, Unit, User
from app.seed import SERVICE_ASSEMBLER_NAME
from app.services.batches import Catalog
from app.services.units import MAX_PRINT_QTY, UnitError, create_printed_units
from app.statuses import IN_STOCK, MV_ACCEPT
from app.transitions import apply_transition

LEGACY_NOTE = "старый остаток без этикетки"
MAX_LEGACY_QTY = 2000  # на один артикул одной площадки — защита от лишнего нуля


@dataclass
class LegacyRow:
    """Строка проверки перед загрузкой."""

    name: str  # как в файле
    shelf: int  # на полке всего
    article: Article | None = None
    site: Site | None = None
    labeled: int = 0  # уже на складе с этикетками
    unlabeled: int = 0  # уже заведено старым остатком
    problem: str = ""

    @property
    def to_create(self) -> int:
        if self.problem or self.article is None or self.site is None:
            return 0
        return max(0, self.shelf - self.labeled - self.unlabeled)

    @property
    def warning(self) -> str:
        if self.problem:
            return self.problem
        have = self.labeled + self.unlabeled
        if self.shelf < have:
            return f"на полке меньше, чем по складу ({have}) — проверьте"
        return ""


def _header_index(header: list[str], *words: str) -> int | None:
    for index, cell in enumerate(header):
        text = cell.lower()
        if any(word in text for word in words):
            return index
    return None


def read_legacy_xlsx(content: bytes) -> list[tuple[str, str, int]]:
    """(артикул, площадка или "", количество) из первого листа. Шапка — в первой строке.

    Нужны столбцы «Артикул» и «Количество» (или «Остаток», «Шт»); «Площадка» — по желанию.
    """
    from openpyxl import load_workbook

    try:
        sheet = load_workbook(BytesIO(content), data_only=True, read_only=True).worksheets[0]
    except Exception as exc:
        raise UnitError("Не удалось прочитать файл: нужен Excel (.xlsx)") from exc
    rows = sheet.iter_rows(values_only=True)
    header = [str(c or "").strip() for c in next(rows, ())]
    col_article = _header_index(header, "артикул")
    col_qty = _header_index(header, "кол", "остат", "шт")
    col_site = _header_index(header, "площад", "производ")
    if col_article is None or col_qty is None:
        raise UnitError("В первой строке нужны столбцы «Артикул» и «Количество»")
    out: list[tuple[str, str, int]] = []
    for row in rows:
        cells = list(row)
        name = str(cells[col_article] or "").strip() if col_article < len(cells) else ""
        raw = cells[col_qty] if col_qty < len(cells) else None
        if not name or raw in (None, ""):
            continue
        try:
            qty = int(float(str(raw).replace(",", ".")))
        except ValueError:
            continue
        site = str(cells[col_site] or "").strip() if col_site is not None and col_site < len(cells) else ""
        if qty > 0:
            out.append((name, site, qty))
    if not out:
        raise UnitError("В файле нет строк с артикулом и количеством")
    return out


def _counts(session: Session, *, unlabeled: bool) -> dict[tuple[int, int], int]:
    rows = session.execute(
        select(Unit.article_id, Unit.site_id, func.count())
        .where(Unit.status == IN_STOCK, Unit.unlabeled.is_(unlabeled))
        .group_by(Unit.article_id, Unit.site_id)
    ).all()
    return {(int(a), int(s)): int(n) for a, s, n in rows}


def legacy_counts(session: Session) -> dict[tuple[int, int], int]:
    """Старых без этикеток на складе: (артикул, площадка) → штук."""
    return _counts(session, unlabeled=True)


def preview_legacy(
    session: Session, lines: list[tuple[str, str, int]], *, default_site: Site | None = None
) -> list[LegacyRow]:
    """Что будет заведено: по каждому артикулу на полке, уже на складе, сколько добавить."""
    catalog = Catalog(session)
    labeled = _counts(session, unlabeled=False)
    old = _counts(session, unlabeled=True)
    merged: dict[tuple[str, int | None, int | None], LegacyRow] = {}
    for name, site_name, qty in lines:
        article = catalog.article(name)
        site = catalog.site(site_name, article.name if article else name) if site_name else None
        if site is None and not site_name:
            site = default_site or catalog.site(None, article.name if article else name)
        key = (name if article is None else "", article.id if article else None, site.id if site else None)
        row = merged.get(key)
        if row is None:
            row = merged[key] = LegacyRow(
                name=article.name if article else name, shelf=0, article=article, site=site
            )
        row.shelf += qty
    for row in merged.values():
        if row.article is None:
            row.problem = "нет в справочнике"
        elif row.site is None:
            row.problem = "не понятно, какая площадка — добавьте столбец «Площадка»"
        elif row.shelf > MAX_LEGACY_QTY:
            row.problem = f"больше {MAX_LEGACY_QTY} — проверьте, нет ли лишнего нуля"
        if row.article is not None and row.site is not None:
            row.labeled = labeled.get((row.article.id, row.site.id), 0)
            row.unlabeled = old.get((row.article.id, row.site.id), 0)
    return sorted(merged.values(), key=lambda r: (r.site.name if r.site else "", r.name))


def _create_legacy(
    session: Session, *, article_id: int, site_id: int, qty: int, actor: User, note: str
) -> int:
    """Завести qty старых зеркал без этикеток на склад площадки."""
    service = session.scalar(select(User).where(User.name == SERVICE_ASSEMBLER_NAME))
    if service is None:
        raise UnitError(f"Нет служебного сотрудника «{SERVICE_ASSEMBLER_NAME}»")
    left = qty
    while left > 0:
        chunk = min(left, MAX_PRINT_QTY)
        units = create_printed_units(
            session, article_id=article_id, qty=chunk, site_id=site_id,
            assembler_id=service.id, actor_id=actor.id,
        )
        for unit in units:
            unit.unlabeled = True
            apply_transition(
                session, unit, IN_STOCK, movement_type=MV_ACCEPT, user_id=actor.id,
                to_site_id=site_id, note=note,
            )
        left -= chunk
    session.flush()
    return qty


def apply_legacy(session: Session, rows: list[LegacyRow], *, actor: User) -> int:
    """Завести старые зеркала без этикеток. Возвращает, сколько заведено."""
    created = 0
    for row in rows:
        if row.to_create and row.article is not None and row.site is not None:
            created += _create_legacy(
                session, article_id=row.article.id, site_id=row.site.id, qty=row.to_create,
                actor=actor, note=LEGACY_NOTE,
            )
    return created


FOUND_NOTE = "нашлось старое без этикетки"
MAX_FOUND_QTY = 10  # больше — это уже пересчёт через «Старый остаток»


def add_found_legacy(session: Session, *, article_name: str, site_id: int, qty: int, actor: User) -> Article:
    """«Нашлось старое»: склад просит нашу этикетку, а в руках старое без неё — начальник добавляет его.

    Так бывает, если раньше отгрузили новое, не отсканировав нашу этикетку: склад списал вместо него старое.
    """
    article = Catalog(session).article(article_name)
    if article is None or article.is_header:
        raise UnitError(f"Артикула «{article_name}» нет в справочнике")
    if not 1 <= qty <= MAX_FOUND_QTY:
        raise UnitError(f"За раз от 1 до {MAX_FOUND_QTY}. Больше — пересчитайте через «Старый остаток»")
    if session.get(Site, site_id) is None:
        raise UnitError("Нет такой площадки")
    _create_legacy(session, article_id=article.id, site_id=site_id, qty=qty, actor=actor, note=FOUND_NOTE)
    return article


def pick_legacy_unit(session: Session, *, article_id: int, site_id: int) -> Unit | None:
    """Взять одно старое зеркало без этикетки (два грузчика сразу не возьмут одно и то же)."""
    return session.scalars(
        select(Unit)
        .where(
            Unit.unlabeled.is_(True), Unit.status == IN_STOCK,
            Unit.article_id == article_id, Unit.site_id == site_id,
        )
        .order_by(Unit.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Unit)
    ).first()


def legacy_articles(session: Session, site_id: int) -> list[tuple[int, str, int]]:
    """Артикулы площадки, у которых есть старые без этикеток: (id, название, штук)."""
    counts: dict[int, int] = defaultdict(int)
    for (article_id, sid), n in legacy_counts(session).items():
        if sid == site_id:
            counts[article_id] += n
    if not counts:
        return []
    articles = session.scalars(
        select(Article).where(Article.id.in_(counts)).order_by(Article.sort_order)
    ).all()
    return [(a.id, a.name, counts[a.id]) for a in articles]
