"""SPEC §12 п. 9: импорт справочника из приложенного TOP.xlsx."""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importer import import_top, parse_top
from app.models import Article

TOP_PATH = Path(__file__).resolve().parent.parent / "docs" / "input" / "TOP.xlsx"

# В файле 306 строк: 296 артикулов и 10 строк-заголовков подгрупп
EXPECTED_ARTICLES = 296
EXPECTED_HEADERS = 10
EXPECTED_ROWS = EXPECTED_ARTICLES + EXPECTED_HEADERS


def test_parse_counts() -> None:
    """Файл разбирается в ожидаемое число строк."""
    rows, report = parse_top(TOP_PATH)
    assert len(rows) == EXPECTED_ROWS
    assert report.headers == EXPECTED_HEADERS
    assert report.duplicates == 0
    assert len([r for r in rows if r.kind == "article"]) == EXPECTED_ARTICLES


def test_import_creates_articles(db: Session) -> None:
    """Импорт создаёт артикулы и строки-заголовки."""
    report = import_top(db, TOP_PATH)
    db.commit()
    assert report.added == EXPECTED_ARTICLES
    assert report.headers == EXPECTED_HEADERS
    articles = db.scalar(select(func.count()).select_from(Article).where(Article.kind == "article"))
    headers = db.scalar(select(func.count()).select_from(Article).where(Article.kind == "header"))
    assert articles == EXPECTED_ARTICLES
    assert headers == EXPECTED_HEADERS


def test_headers_are_not_articles(db: Session) -> None:
    """Заголовки подгрупп не попали в артикулы."""
    import_top(db, TOP_PATH)
    db.commit()
    names = set(db.scalars(select(Article.name).where(Article.kind == "article")).all())
    assert "ПОЛОТНА" not in names
    assert "БЕЗРАМНЫЕ КРУГЛЫЕ" not in names
    header_names = set(db.scalars(select(Article.name).where(Article.kind == "header")).all())
    assert "ПОЛОТНА" in header_names
    assert "БЕЗРАМНЫЕ КРУГЛЫЕ" in header_names


def test_subgroups_filled(db: Session) -> None:
    """Подписи подгрупп из столбца B действуют до следующей подписи."""
    import_top(db, TOP_PATH)
    db.commit()
    first = db.scalar(select(Article).where(Article.name_norm == "zerkalo_100x200"))
    assert first is not None
    assert first.subgroup == "полотна"
    bp = db.scalar(select(Article).where(Article.name_norm == "zerkalo_100x200_bp"))
    assert bp is not None
    assert bp.subgroup == "полотна без креплений"
    noga = db.scalar(select(Article).where(Article.name_norm == "alum noga 170x70 black"))
    assert noga is not None
    assert noga.subgroup == "алюминий на ноге"


def test_groups_and_prefixes(db: Session) -> None:
    """Группа берётся из листа, префикс — из артикула."""
    import_top(db, TOP_PATH)
    db.commit()
    alum = db.scalar(select(Article).where(Article.name_norm == "alum rama 200x100 black"))
    assert alum is not None and alum.group == "Алюминий" and alum.prefix == "A"
    ambient = db.scalar(select(Article).where(Article.name_norm == "ambient 200x100 knopka 4000"))
    assert ambient is not None and ambient.group == "Безрамные" and ambient.prefix == "AT"


def test_sort_order_matches_file(db: Session) -> None:
    """Порядок строк в базе совпадает с порядком в файле."""
    rows, _ = parse_top(TOP_PATH)
    import_top(db, TOP_PATH)
    db.commit()
    in_db = db.scalars(select(Article).order_by(Article.sort_order)).all()
    assert [a.name for a in in_db] == [r.name for r in rows]


def test_colors_imported(db: Session) -> None:
    """Цвет строки берётся из заливки ячейки (цвета темы — ближайший из палитры)."""
    import_top(db, TOP_PATH)
    db.commit()
    polotno = db.scalar(select(Article).where(Article.name_norm == "zerkalo_100x200"))
    alum = db.scalar(select(Article).where(Article.name_norm == "alum rama 200x100 black"))
    ambient = db.scalar(select(Article).where(Article.name_norm == "ambient 200x100 knopka 4000"))
    assert polotno is not None and polotno.color_hex == "#FFE0C1"
    assert alum is not None and alum.color_hex == "#DCE0F8"
    assert ambient is not None and ambient.color_hex == "#B7E1CD"


def test_reimport_does_not_duplicate(db: Session) -> None:
    """Повторный импорт не создаёт дублей и ничего не теряет."""
    import_top(db, TOP_PATH)
    db.commit()
    report = import_top(db, TOP_PATH)
    db.commit()
    assert report.added == 0
    assert report.existed == EXPECTED_ARTICLES
    total = db.scalar(select(func.count()).select_from(Article))
    assert total == EXPECTED_ROWS


def test_reimport_keeps_missing_articles(db: Session) -> None:
    """Артикулы, которых нет в файле, при повторном импорте не удаляются."""
    import_top(db, TOP_PATH)
    db.commit()
    db.add(
        Article(
            kind="article",
            name="Старый артикул",
            name_norm="старый артикул",
            group="Полотна",
            prefix="X",
            sort_order=9999,
        )
    )
    db.commit()
    import_top(db, TOP_PATH)
    db.commit()
    kept = db.scalar(select(Article).where(Article.name_norm == "старый артикул"))
    assert kept is not None
