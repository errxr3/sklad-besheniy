"""SPEC §12 п. 10: доступ к маршрутам проверяется на сервере по таблице ролей."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.roles import API_KEY, NEEDS_OPEN_SHIFT, PUBLIC, ROUTE_ROLES
from app.statuses import ALL_ROLES
from tests.helpers import get_csrf, login, make_user, open_shift

# Подстановка значений вместо параметров пути: маршрут должен отвечать проверкой доступа,
# а не падать на разборе пути.
PATH_PARAMS: dict[str, str] = {
    "number": "Z0000001",
    "unit_number": "Z0000001",
    "site_id": "1",
    "user_id": "1",
    "pallet_id": "1",
    "task_id": "1",
    "inventory_id": "1",
    "article_id": "1",
    "shift_id": "1",
    "key": "lead",
}


def registered_routes() -> set[tuple[str, str]]:
    """Все маршруты приложения (метод, путь)."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            found.add((method, path))
    return found


def fill_path(path: str) -> str:
    """Подставить значения вместо {параметров} в пути."""
    result = path
    for name, value in PATH_PARAMS.items():
        result = result.replace("{" + name + "}", value)
    assert "{" not in result, f"не заполнен параметр пути: {path}"
    return result


def test_every_route_is_described() -> None:
    """Каждый маршрут приложения описан в таблице ролей."""
    missing = registered_routes() - set(ROUTE_ROLES)
    assert not missing, f"маршруты без описания доступа: {sorted(missing)}"


def test_no_stale_rows_in_table() -> None:
    """В таблице ролей нет описаний для несуществующих маршрутов."""
    stale = set(ROUTE_ROLES) - registered_routes()
    assert not stale, f"лишние строки в таблице ролей: {sorted(stale)}"


@pytest.mark.parametrize(("method", "path"), sorted(ROUTE_ROLES))
def test_anonymous_cannot_open_protected_routes(
    method: str, path: str, client: TestClient
) -> None:
    """Без входа закрыты все маршруты, кроме /login и /health (SPEC §4)."""
    allowed = ROUTE_ROLES[(method, path)]
    if allowed == PUBLIC:
        return
    response = client.request(method, fill_path(path), follow_redirects=False)
    if allowed == API_KEY:
        # Внешнее API отвечает 401: нужен ключ в заголовке X-API-Key
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        return
    assert response.status_code in (303, 401), f"{method} {path} -> {response.status_code}"
    if response.status_code == 303:
        assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize(("method", "path"), sorted(ROUTE_ROLES))
def test_forbidden_roles_get_403(method: str, path: str, client: TestClient, db: Session) -> None:
    """Запрещённой роли маршрут отвечает 403, разрешённой — нет."""
    allowed = ROUTE_ROLES[(method, path)]
    if allowed in (PUBLIC, API_KEY):
        # Доступ к внешнему API определяется ключом, а не ролью — см. test_api_v1.py
        return
    url = fill_path(path)

    for index, role in enumerate(ALL_ROLES):
        user = make_user(db, name=f"Сотрудник {role} {index}", role=role, pin="1234")
        if (method, path) in NEEDS_OPEN_SHIFT:
            # Маршрут требует открытой смены — иначе сборщик и грузчик получат 403 по другой причине
            open_shift(db, user)
        with TestClient(app) as role_client:
            login(role_client, user)
            token = get_csrf(role_client, "/")
            if method == "GET":
                response = role_client.get(url, follow_redirects=False)
            else:
                response = role_client.request(
                    method,
                    url,
                    data={"csrf_token": token},
                    headers={"x-csrf-token": token},
                    follow_redirects=False,
                )
            if role in allowed:
                assert response.status_code != 403, (
                    f"{role} должен иметь доступ к {method} {path}, получил 403"
                )
                # 500 здесь означал бы ошибку в обработчике или шаблоне
                assert response.status_code != 500, (
                    f"{method} {path} для роли {role} ответил 500"
                )
            else:
                assert response.status_code == 403, (
                    f"{role} не должен иметь доступ к {method} {path}, "
                    f"получил {response.status_code}"
                )
