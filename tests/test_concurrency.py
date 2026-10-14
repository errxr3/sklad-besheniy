"""SPEC §12 п. 3 и 4: одновременные сканы и одновременная печать."""

import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models import Movement, Unit
from app.services.units import accept_unit, create_printed_units
from app.statuses import IN_STOCK, MV_ACCEPT, ROLE_ASSEMBLER
from tests.helpers import make_article, make_user, site_by_name


def test_two_simultaneous_scans_give_one_movement(db: Session) -> None:
    """Два одновременных скана одного зеркала → ровно одна запись в журнале."""
    worker = make_user(db, name="Сборщик гонки", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    article = make_article(db)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )[0]
    db.commit()
    number = unit.number
    worker_id = worker.id

    start = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def scan() -> None:
        """Скан в отдельном соединении."""
        session = get_session_factory()()
        try:
            from app.models import User

            actor = session.get(User, worker_id)
            assert actor is not None
            start.wait(timeout=10)
            result = accept_unit(session, code=number, actor=actor)
            session.commit()
            with lock:
                results.append(result.ok)
        finally:
            session.close()

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == [False, True], f"ожидали один успех, получили {results}"

    db.expire_all()
    accepted = db.scalar(
        select(func.count())
        .select_from(Movement)
        .where(Movement.unit_id == unit.id, Movement.type == MV_ACCEPT)
    )
    assert accepted == 1
    db.refresh(unit)
    assert unit.status == IN_STOCK


def test_two_printers_get_unique_numbers(db: Session) -> None:
    """Два сотрудника печатают по 20 этикеток → 40 разных номеров без повторов."""
    first = make_user(db, name="Печатник 1", role=ROLE_ASSEMBLER)
    second = make_user(db, name="Печатник 2", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    article = make_article(db)
    db.commit()
    ids = (first.id, second.id)
    article_id, site_id = article.id, site.id

    start = threading.Barrier(2)
    numbers: list[str] = []
    lock = threading.Lock()

    def print_batch(user_id: int) -> None:
        """Печать 20 этикеток в своём соединении."""
        session = get_session_factory()()
        try:
            start.wait(timeout=10)
            units = create_printed_units(
                session,
                article_id=article_id,
                qty=20,
                site_id=site_id,
                assembler_id=user_id,
                actor_id=user_id,
            )
            session.commit()
            with lock:
                numbers.extend(unit.number for unit in units)
        finally:
            session.close()

    threads = [threading.Thread(target=print_batch, args=(user_id,)) for user_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(numbers) == 40
    assert len(set(numbers)) == 40, "номера повторились"

    db.expire_all()
    in_db = db.scalars(select(Unit.number)).all()
    assert len(set(in_db)) == 40
