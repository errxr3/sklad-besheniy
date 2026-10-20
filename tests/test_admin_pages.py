"""Этап 1: страницы администратора — сотрудники, площадки, справочник, параметры."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdminLog, Article, Site, User
from app.settings_store import get_flag, get_int
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_STOREKEEPER
from tests.helpers import get_csrf, login, make_user

TOP_PATH = Path(__file__).resolve().parent.parent / "docs" / "input" / "TOP.xlsx"


def _admin(client: TestClient, db: Session) -> User:
    """Войти администратором."""
    user = make_user(db, name="Главный", role=ROLE_ADMIN, pin="1234")
    login(client, user)
    return user


def test_admin_creates_employee(client: TestClient, db: Session) -> None:
    """Администратор создаёт сотрудника, действие попадает в журнал администрирования."""
    _admin(client, db)
    token = get_csrf(client, "/admin/employees")
    response = client.post(
        "/admin/employees/create",
        data={
            "csrf_token": token,
            "name": "Грузчик Вася",
            "role": ROLE_LOADER,
            "pin": "5678",
            "home_site_id": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    created = db.scalar(select(User).where(User.name == "Грузчик Вася"))
    assert created is not None and created.role == ROLE_LOADER
    logged = db.scalar(select(AdminLog).where(AdminLog.action == "создан сотрудник"))
    assert logged is not None


def test_new_employee_can_login(client: TestClient, db: Session) -> None:
    """У созданного сотрудника работает вход с заданным PIN."""
    _admin(client, db)
    token = get_csrf(client, "/admin/employees")
    client.post(
        "/admin/employees/create",
        data={"csrf_token": token, "name": "Сборщик Коля", "role": ROLE_ASSEMBLER, "pin": "4455"},
    )
    created = db.scalar(select(User).where(User.name == "Сборщик Коля"))
    assert created is not None
    with TestClient(client.app) as fresh:
        login(fresh, created, pin="4455")
        assert fresh.get("/").status_code == 200


def test_short_pin_rejected(client: TestClient, db: Session) -> None:
    """PIN короче 4 цифр не принимается."""
    _admin(client, db)
    token = get_csrf(client, "/admin/employees")
    response = client.post(
        "/admin/employees/create",
        data={"csrf_token": token, "name": "Плохой PIN", "role": ROLE_ASSEMBLER, "pin": "12"},
        follow_redirects=True,
    )
    assert "PIN" in response.text
    assert db.scalar(select(User).where(User.name == "Плохой PIN")) is None


def test_role_change_and_deactivation(client: TestClient, db: Session) -> None:
    """Смена роли и деактивация сотрудника."""
    _admin(client, db)
    worker = make_user(db, name="Работник", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client, "/admin/employees")
    client.post(
        "/admin/employees/update",
        data={
            "csrf_token": token,
            "user_id": str(worker.id),
            "name": "Работник",
            "role": ROLE_LOADER,
            "active": "0",
            "home_site_id": "",
        },
    )
    db.refresh(worker)
    assert worker.role == ROLE_LOADER
    assert worker.active is False


def test_reset_pin_unlocks_user(client: TestClient, db: Session) -> None:
    """Сброс PIN снимает блокировку входа."""
    from datetime import timedelta

    from app.timeutil import now_utc

    _admin(client, db)
    worker = make_user(db, name="Забывчивый", role=ROLE_ASSEMBLER, pin="1111")
    worker.locked_until = now_utc() + timedelta(minutes=10)
    worker.failed_logins = 5
    db.commit()

    token = get_csrf(client, "/admin/employees")
    client.post(
        "/admin/employees/reset-pin",
        data={"csrf_token": token, "user_id": str(worker.id), "pin": "9876"},
    )
    db.refresh(worker)
    assert worker.locked_until is None
    assert worker.failed_logins == 0
    with TestClient(client.app) as fresh:
        login(fresh, worker, pin="9876")


def test_site_create_and_update(client: TestClient, db: Session) -> None:
    """Добавление площадки и включение геопроверки."""
    _admin(client, db)
    token = get_csrf(client, "/admin/sites")
    client.post("/admin/sites/create", data={"csrf_token": token, "name": "Новая площадка"})
    site = db.scalar(select(Site).where(Site.name == "Новая площадка"))
    assert site is not None

    client.post(
        "/admin/sites/update",
        data={
            "csrf_token": token,
            "site_id": str(site.id),
            "name": "Новая площадка",
            "active": "1",
            "sort_order": "5",
            "geo_lat": "56.3287",
            "geo_lon": "44.0020",
            "geo_radius_m": "250",
            "geo_check_enabled": "1",
            "labeling_done": "1",
        },
    )
    db.refresh(site)
    assert site.geo_check_enabled is True
    assert site.geo_radius_m == 250
    assert site.labeling_done is True
    assert site.sort_order == 5


def test_duplicate_site_rejected(client: TestClient, db: Session) -> None:
    """Площадка с существующим названием не создаётся."""
    _admin(client, db)
    token = get_csrf(client, "/admin/sites")
    response = client.post(
        "/admin/sites/create", data={"csrf_token": token, "name": "Коминтерна"}, follow_redirects=True
    )
    assert "уже есть" in response.text


def test_articles_import_through_page(client: TestClient, db: Session) -> None:
    """Импорт TOP.xlsx со страницы настроек."""
    _admin(client, db)
    token = get_csrf(client, "/admin/articles")
    with TOP_PATH.open("rb") as handle:
        response = client.post(
            "/admin/articles/import",
            data={"csrf_token": token},
            files={"file": ("TOP.xlsx", handle.read(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "Добавлено: 298" in response.text
    count = len(db.scalars(select(Article).where(Article.kind == "article")).all())
    assert count == 298


def test_article_deactivation_and_color(client: TestClient, db: Session) -> None:
    """Артикул можно отключить, а цвет подгруппы — поменять."""
    from app.importer import import_top

    _admin(client, db)
    import_top(db, TOP_PATH)
    db.commit()
    article = db.scalar(select(Article).where(Article.name_norm == "zerkalo_100x200"))
    assert article is not None

    token = get_csrf(client, "/admin/articles")
    client.post(
        "/admin/articles/toggle",
        data={"csrf_token": token, "article_id": str(article.id), "active": "0"},
    )
    db.refresh(article)
    assert article.active is False

    client.post(
        "/admin/articles/color",
        data={"csrf_token": token, "group": "Полотна", "subgroup": "полотна", "color_hex": "#AABBCC"},
    )
    db.refresh(article)
    assert article.color_hex == "#AABBCC"


def test_settings_saved(client: TestClient, db: Session) -> None:
    """Размер этикетки и переключатели этапов сохраняются."""
    _admin(client, db)
    token = get_csrf(client, "/admin/settings")
    client.post(
        "/admin/settings",
        data={
            "csrf_token": token,
            "label_width_mm": "70",
            "label_height_mm": "50",
            "require_mp_barcode": "1",
            "verify_order": "1",
            "ship_cancel_minutes": "15",
        },
    )
    db.expire_all()
    assert get_int(db, "label_width_mm") == 70
    assert get_int(db, "label_height_mm") == 50
    assert get_flag(db, "require_mp_barcode") is True
    assert get_flag(db, "verify_order") is True
    assert get_int(db, "ship_cancel_minutes") == 15


def test_menu_differs_by_role(client: TestClient, db: Session) -> None:
    """Каждая роль видит только своё меню (SPEC §4)."""
    loader = make_user(db, name="Грузчик", role=ROLE_LOADER, pin="1234")
    with TestClient(client.app) as loader_client:
        login(loader_client, loader)
        text = loader_client.get("/").text
        assert "Мой день" in text
        assert "Зеркало" not in text
        assert "Настройки" not in text

    assembler = make_user(db, name="Сборщик", role=ROLE_ASSEMBLER, pin="1234")
    with TestClient(client.app) as asm_client:
        login(asm_client, assembler)
        text = asm_client.get("/").text
        # Сборщику оставлен только «Мой день» со сканом зеркала (решение владельца 2026-10-20)
        assert "Мой день" in text
        assert 'href="/make"' not in text
        assert 'href="/stock"' not in text
        assert 'href="/mirror"' not in text
        assert "Настройки" not in text

    keeper = make_user(db, name="Кладовщик", role=ROLE_STOREKEEPER, pin="1234")
    with TestClient(client.app) as keeper_client:
        login(keeper_client, keeper)
        response = keeper_client.get("/")
        assert str(response.url).endswith("/labels")
        assert "Печать по задачам" in response.text
        assert 'href="/tasks"' not in response.text
        assert "Настройки" not in response.text

    _admin(client, db)
    admin_text = client.get("/").text
    assert "Настройки" in admin_text
