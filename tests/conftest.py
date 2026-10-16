"""Общие фикстуры тестов. Тесты идут против настоящего PostgreSQL (SPEC §2)."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Таблицы, которые чистятся между тестами (все, кроме alembic_version)
TABLES = (
    "movements",
    "inventory_scans",
    "inventories",
    "task_log",
    "tasks",
    "units",
    "pallets",
    "shifts",
    "mp_shipments",
    "api_keys",
    "admin_log",
    "settings",
    "articles",
    "users",
    "sites",
)


def _test_database_url() -> str:
    """Строка подключения к тестовой базе.

    Берётся из TEST_DATABASE_URL, иначе из DATABASE_URL с суффиксом `_test`
    у имени базы, иначе локальный PostgreSQL по умолчанию.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/sklad")
    url = make_url(base)
    name = url.database or "sklad"
    if not name.endswith("_test"):
        name = f"{name}_test"
    return url.set(database=name).render_as_string(hide_password=False)


def _ensure_database(url_str: str) -> None:
    """Создать тестовую базу, если её нет."""
    url = make_url(url_str)
    admin_url = url.set(database="postgres")
    engine = create_engine(admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> Iterator[str]:
    """Создать тестовую базу с нуля и применить миграции."""
    url = _test_database_url()
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ["SESSION_COOKIE_SECURE"] = "0"
    os.environ["SCHEDULER_ENABLED"] = "0"

    _ensure_database(url)

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=str(PROJECT_ROOT))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Миграции не применились:\n{result.stdout}\n{result.stderr}")

    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    yield url


@pytest.fixture(autouse=True)
def clean_db(_prepare_database: str) -> Iterator[None]:
    """Пустая база перед каждым тестом."""
    from app.db import get_engine, reset_engine

    reset_engine()
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE movements DISABLE TRIGGER USER"))
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE movements ENABLE TRIGGER USER"))
        conn.execute(text("ALTER SEQUENCE unit_number_seq RESTART WITH 1"))
        conn.commit()

    # Начальные данные (площадки, служебный сборщик, настройки) — как после миграций
    from app.db import session_scope
    from app.seed import apply_seed

    with session_scope() as session:
        apply_seed(session)
    yield


@pytest.fixture
def client() -> Iterator["TestClient"]:
    """HTTP-клиент приложения."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db() -> Iterator[Session]:
    """Сессия SQLAlchemy для теста."""
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()
