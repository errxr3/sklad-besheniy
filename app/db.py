"""Подключение к PostgreSQL: движок, фабрика сессий, базовый класс моделей."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

# Последовательность номеров зеркал — одна общая на всю систему (SPEC §7)
UNIT_NUMBER_SEQUENCE = "unit_number_seq"


def get_engine() -> Engine:
    """Движок SQLAlchemy (создаётся один раз на процесс)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Фабрика сессий."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _session_factory


def reset_engine() -> None:
    """Сбросить движок и фабрику (нужно тестам при смене строки подключения)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Generator[Session, None, None]:
    """Зависимость FastAPI: сессия на запрос, откат при ошибке."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Сессия для CLI и фоновых задач."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
