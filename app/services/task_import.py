"""Задачи из Excel: сверху имена сотрудников, слева артикулы как в TOP, в клетках количество.

Имена колонок подбираются к сотрудникам площадки. Если всё узнано, задачи ставятся сразу;
иначе показывается проверка, и ничего не записывается до подтверждения.
"""

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.articles import normalize_article
from app.models import Article, Site, User
from app.statuses import ROLE_ASSEMBLER

MAX_FILE_ROWS = 1000
MAX_FILE_COLUMNS = 200
TOTAL_TITLES = {"общее", "итого", "всего", "сумма"}


class TasksFileError(Exception):
    """Файл не похож на таблицу задач, текст показывается пользователю."""


def _norm_person(value: str) -> str:
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[.,;:_\-]+", " ", text)
    return " ".join(text.split())


def _person_score(header: str, name: str) -> float:
    """Насколько заголовок колонки похож на имя сотрудника (0..1)."""
    h, n = _norm_person(header), _norm_person(name)
    if not h or not n:
        return 0.0
    if h == n:
        return 1.0
    h_tokens, n_tokens = h.split(), n.split()
    # «Сергей Б» → «Сергей Беляев», «Г.Кирилл» → «Кирилл Громов»: каждое слово заголовка
    # начинает какое-то слово имени, разные слова заголовка — разные слова имени
    used: set[int] = set()
    ok = True
    for token in h_tokens:
        hit = next((i for i, t in enumerate(n_tokens) if i not in used and t.startswith(token)), None)
        if hit is None:
            ok = False
            break
        used.add(hit)
    if ok:
        return 0.95
    best = SequenceMatcher(None, h, n).ratio()
    if len(h_tokens) == 1:
        # Одно слово («Генадий») сравниваем с каждым словом имени; «Сергей П» с «Сергей Беляев» нет
        for token in n_tokens:
            best = max(best, SequenceMatcher(None, h, token).ratio())
    return min(best, 0.94)  # от 0.95 — только точное совпадение или начало слов


@dataclass
class ImportColumn:
    """Колонка файла с хотя бы одной задачей."""

    index: int
    title: str
    total: int
    user_id: int | None = None  # подобранный сотрудник
    note: str = ""  # «точное совпадение», «похоже», «не найден»
    ranked: list[tuple[int, str]] = field(default_factory=list)  # кого предложить в списке


@dataclass
class ImportCell:
    """Одна задача из файла: колонка, артикул, количество."""

    col: int
    article_id: int
    article_name: str
    label: str
    qty: int


@dataclass
class ImportPreview:
    """Разбор файла до постановки задач."""

    columns: list[ImportColumn] = field(default_factory=list)
    cells: list[ImportCell] = field(default_factory=list)
    unknown: list[tuple[str, int]] = field(default_factory=list)  # (как в файле, штук)
    bad_cells: list[str] = field(default_factory=list)
    repeated_titles: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Всё узнано: можно ставить задачи без проверки."""
        return (
            not self.unknown
            and not self.bad_cells
            and all(c.user_id is not None and c.note != "похоже, проверьте" for c in self.columns)
        )

    @property
    def total_qty(self) -> int:
        return sum(cell.qty for cell in self.cells)

    def payload(self) -> str:
        return json.dumps(
            [
                {"col": c.col, "article_id": c.article_id, "qty": c.qty, "label": c.label}
                for c in self.cells
            ],
            ensure_ascii=False,
        )


def site_assemblers(session: Session, site: Site) -> list[User]:
    """Сборщики, которым можно ставить задачи на площадке (с этой площадкой или без)."""
    return list(
        session.scalars(
            select(User)
            .where(
                User.active.is_(True),
                User.role == ROLE_ASSEMBLER,
                User.is_service.is_(False),
                (User.home_site_id == site.id) | User.home_site_id.is_(None),
            )
            .order_by(User.name)
        ).all()
    )


def parse_tasks_file(session: Session, content: bytes, site: Site) -> ImportPreview:
    """Прочитать xlsx: первая строка — имена, первый столбец — артикулы."""
    try:
        sheet = load_workbook(BytesIO(content), data_only=True, read_only=True).worksheets[0]
    except Exception as exc:  # файл не xlsx, повреждён, запаролен
        raise TasksFileError("Не получилось открыть файл: нужен Excel (.xlsx)") from exc
    rows = list(sheet.iter_rows(values_only=True, max_row=MAX_FILE_ROWS))
    if len(rows) < 2:
        raise TasksFileError("В файле нет строк с артикулами")
    header = rows[0][:MAX_FILE_COLUMNS]

    catalog = {
        a.name_norm: a
        for a in session.scalars(
            select(Article).where(Article.kind == "article", Article.active.is_(True))
        ).all()
        if a.name_norm
    }
    workers = site_assemblers(session, site)
    preview = ImportPreview()

    titles: dict[int, str] = {}
    for idx, value in enumerate(header):
        if idx == 0 or value is None or not str(value).strip():
            continue
        if _norm_person(str(value)) in TOTAL_TITLES:
            continue  # столбец с суммой, не сотрудник
        titles[idx] = str(value).strip()

    totals: dict[int, int] = {}
    unknown: dict[str, int] = {}
    for row_no, row in enumerate(rows[1:], start=2):
        label_raw = row[0] if row else None
        if label_raw is None or not str(label_raw).strip():
            continue
        label = " ".join(str(label_raw).split())
        values = [
            (i, v) for i, v in enumerate(row) if i in titles and v is not None and str(v).strip() != ""
        ]
        if not values:
            continue
        article = catalog.get(normalize_article(label))
        for idx, raw in values:
            qty = _to_qty(raw)
            if qty is None:
                preview.bad_cells.append(f"строка {row_no}, «{titles[idx]}»: «{raw}» не число")
                continue
            if qty == 0:
                continue
            if article is None:
                unknown[label] = unknown.get(label, 0) + qty
                continue
            preview.cells.append(ImportCell(idx, article.id, article.name, label, qty))
            totals[idx] = totals.get(idx, 0) + qty
    preview.unknown = sorted(unknown.items())

    # Колонки: подбираем сотрудника, одинаковые заголовки считаем одним человеком
    by_title: dict[str, int | None] = {}
    for idx in sorted(totals):
        key = _norm_person(titles[idx])
        column = ImportColumn(idx, titles[idx], totals[idx])
        scored = sorted(((_person_score(titles[idx], w.name), w) for w in workers), key=lambda s: -s[0])
        column.ranked = [(w.id, w.name) for _, w in scored]
        best = [w for s, w in scored if s >= 0.95]
        fuzzy = [w for s, w in scored if 0.85 <= s < 0.95]
        if key in by_title and by_title[key] is not None:
            column.user_id = by_title[key]
            column.note = "тот же человек, что в соседней колонке"
            preview.repeated_titles.append(titles[idx])
        elif len(best) == 1:
            column.user_id = best[0].id
            column.note = "найден"
        elif not best and len(fuzzy) == 1:
            column.user_id = fuzzy[0].id
            column.note = "похоже, проверьте"
        else:
            column.note = "не найден" if not best else "подходит несколько, выберите"
        by_title.setdefault(key, column.user_id)
        preview.columns.append(column)
    if not preview.cells and not preview.unknown:
        raise TasksFileError("В файле нет задач: в клетках с количеством пусто")
    return preview


def _to_qty(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return int(raw) if float(raw).is_integer() and raw >= 0 else None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None
