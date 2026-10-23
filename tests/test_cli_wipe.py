"""Очистка базы перед запуском: остаются администраторы, справочник и площадки (2026-10-23)."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cli import main
from app.models import Article, Movement, Site, Unit, User
from app.services.units import create_printed_units
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER
from tests.helpers import make_article, make_user, site_by_name


def test_wipe_keeps_admin_catalog_and_sites(db: Session) -> None:
    admin = make_user(db, name="Администратор", role=ROLE_ADMIN)
    worker = make_user(db, name="Сборщик", role=ROLE_ASSEMBLER)
    make_user(db, name="Грузчик", role=ROLE_LOADER)
    article = make_article(db, name="Zerkalo_60x160")
    site = site_by_name(db)
    create_printed_units(
        db, article_id=article.id, qty=3, site_id=site.id, assembler_id=worker.id, actor_id=admin.id
    )
    db.commit()
    sites_before = db.scalar(select(func.count()).select_from(Site))
    db.rollback()  # очистке нужна исключительная блокировка таблиц, своя транзакция мешает

    assert main(["wipe-data"]) == 2  # без подтверждения ничего не трогаем
    assert db.scalar(select(func.count()).select_from(Unit)) == 3
    db.rollback()

    assert main(["wipe-data", "--confirm", "ОЧИСТИТЬ"]) == 0
    db.expire_all()
    people = db.scalars(select(User)).all()
    assert {u.name for u in people if not u.is_service} == {"Администратор"}
    assert any(u.is_service for u in people)  # служебный сборщик «Остаток на запуск» нужен
    assert db.scalar(select(func.count()).select_from(Unit)) == 0
    assert db.scalar(select(func.count()).select_from(Movement)) == 0
    assert db.get(Article, article.id) is not None
    assert db.scalar(select(func.count()).select_from(Site)) == sites_before

    # Журнал после очистки снова защищён от удаления
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().connect() as conn:
        disabled = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'movements'::regclass"
                " AND NOT tgisinternal AND tgenabled = 'D'"
            )
        ).scalar_one()
    assert disabled == 0


def test_wipe_refuses_without_admin(db: Session) -> None:
    make_user(db, name="Сборщик", role=ROLE_ASSEMBLER)
    db.commit()
    assert main(["wipe-data", "--confirm", "ОЧИСТИТЬ"]) == 1
    assert db.scalar(select(func.count()).select_from(User).where(User.name == "Сборщик")) == 1
