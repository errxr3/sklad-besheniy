"""SPEC §12 п. 14 и 17: вход по логину и паролю, блокировка после 5 ошибок, защита CSRF."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER
from app.timeutil import now_utc
from tests.helpers import get_csrf, login, make_user


def test_login_success_and_logout(client: TestClient, db: Session) -> None:
    """Правильный PIN пускает в систему, «Выйти» завершает сессию."""
    user = make_user(db, name="Пётр", role=ROLE_ASSEMBLER, pin="4321")
    login(client, user, pin="4321")
    home = client.get("/")
    assert home.status_code == 200
    assert "Мой день" in home.text

    token = get_csrf(client, "/")
    out = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_pin_shows_attempts_left(client: TestClient, db: Session) -> None:
    """Неверный PIN показывает, сколько попыток осталось."""
    user = make_user(db, name="Анна", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"login": user.login, "password": "2222", "next": "/", "csrf_token": token},
    )
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.text
    assert "Осталось попыток: 4" in response.text


def test_lock_after_five_wrong_pins(client: TestClient, db: Session) -> None:
    """После 5 неверных PIN вход блокируется на 15 минут (SPEC §12 п. 14)."""
    user = make_user(db, name="Сергей", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    for _ in range(5):
        client.post(
            "/login",
            data={"login": user.login, "password": "9999", "next": "/", "csrf_token": token},
        )

    db.expire_all()
    db.refresh(user)
    assert user.locked_until is not None
    left = user.locked_until - now_utc()
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)

    # Даже верный PIN теперь не пускает
    response = client.post(
        "/login",
        data={"login": user.login, "password": "1111", "next": "/", "csrf_token": token},
    )
    assert "заблокирован" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_unlock_after_fifteen_minutes(client: TestClient, db: Session) -> None:
    """После истечения 15 минут вход снова работает."""
    user = make_user(db, name="Ольга", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    for _ in range(5):
        client.post(
            "/login",
            data={"login": user.login, "password": "9999", "next": "/", "csrf_token": token},
        )
    db.refresh(user)
    assert user.locked_until is not None

    # Переводим время блокировки в прошлое — как будто 15 минут прошли
    user.locked_until = now_utc() - timedelta(seconds=1)
    db.commit()

    response = client.post(
        "/login",
        data={"login": user.login, "password": "1111", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.refresh(user)
    assert user.locked_until is None
    assert user.failed_logins == 0


def test_counter_resets_after_success(client: TestClient, db: Session) -> None:
    """Счётчик неудач обнуляется после успешного входа."""
    user = make_user(db, name="Мария", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    for _ in range(3):
        client.post(
            "/login",
            data={"login": user.login, "password": "9999", "next": "/", "csrf_token": token},
        )
    db.refresh(user)
    assert user.failed_logins == 3
    login(client, user, pin="1111")
    db.refresh(user)
    assert user.failed_logins == 0


def test_inactive_user_cannot_login(client: TestClient, db: Session) -> None:
    """Отключённый сотрудник не входит."""
    user = make_user(db, name="Уволенный", role=ROLE_ASSEMBLER, pin="1111", active=False)
    make_user(db, name="Работающий", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"login": user.login, "password": "1111", "next": "/", "csrf_token": token},
    )
    assert "Неверный логин или пароль" in response.text


def test_service_user_cannot_login(client: TestClient, db: Session) -> None:
    """Служебный сборщик «Остаток на запуск» не может войти."""
    from sqlalchemy import select

    from app.models import User

    service = db.scalar(select(User).where(User.is_service.is_(True)))
    assert service is not None
    make_user(db, name="Работающий", role=ROLE_ASSEMBLER, pin="1111")
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"login": service.login, "password": "1234", "next": "/", "csrf_token": token},
    )
    assert "Неверный логин или пароль" in response.text


def test_login_page_shows_nobody(client: TestClient, db: Session) -> None:
    """На экране входа нет списка: не видно ни имён, ни должностей."""
    make_user(db, name="Обычный Сборщик", role=ROLE_ASSEMBLER)
    page = client.get("/login").text
    assert "Обычный" not in page and "Сборщик зеркал" not in page and "Остаток на запуск" not in page
    assert 'name="login"' in page and 'name="password"' in page


def test_login_case_insensitive_and_letters_in_password(client: TestClient, db: Session) -> None:
    """Логин без учёта регистра, пароль может быть с буквами."""
    user = make_user(db, name="Пётр Иванов", role=ROLE_ASSEMBLER, pin="Склад-2026!", login="Petrov")
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"login": "PETROV", "password": "Склад-2026!", "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert user.login == "Petrov"


def test_unknown_login_looks_like_wrong_password(client: TestClient, db: Session) -> None:
    """Про несуществующий логин сайт говорит то же, что про неверный пароль."""
    token = get_csrf(client)
    response = client.post(
        "/login", data={"login": "nobody", "password": "1234", "next": "/", "csrf_token": token}
    )
    assert "Неверный логин или пароль" in response.text


def test_post_without_csrf_rejected(client: TestClient, db: Session) -> None:
    """POST без CSRF-токена отклоняется (SPEC §12 п. 17)."""
    user = make_user(db, name="Админ", role=ROLE_ADMIN, pin="1234")
    login(client, user)
    response = client.post(
        "/admin/employees/create",
        data={"name": "Новый", "role": ROLE_ASSEMBLER, "pin": "5555"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "Форма устарела" in response.text


def test_post_with_wrong_csrf_rejected(client: TestClient, db: Session) -> None:
    """Чужой CSRF-токен не принимается."""
    user = make_user(db, name="Админ2", role=ROLE_ADMIN, pin="1234")
    login(client, user)
    response = client.post(
        "/admin/employees/create",
        data={"name": "Новый", "role": ROLE_ASSEMBLER, "pin": "5555", "csrf_token": "подделка"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_login_post_without_csrf_rejected(client: TestClient, db: Session) -> None:
    """Даже вход требует CSRF-токен."""
    user = make_user(db, name="Без токена", role=ROLE_ASSEMBLER, pin="1234")
    client.get("/login")
    response = client.post(
        "/login", data={"login": user.login, "password": "1234", "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 403
