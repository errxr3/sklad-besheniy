"""Помощники тестов: создание сотрудников и вход."""

import re

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, Site, User
from app.security import hash_pin
from app.statuses import ROLE_ADMIN

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_user(
    db: Session,
    *,
    name: str = "Иван",
    role: str = ROLE_ADMIN,
    pin: str = "1234",
    active: bool = True,
    home_site_id: int | None = None,
) -> User:
    """Создать сотрудника с известным PIN."""
    user = User(
        name=name,
        role=role,
        pin_hash=hash_pin(pin),
        active=active,
        home_site_id=home_site_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def site_by_name(db: Session, name: str = "Коминтерна") -> Site:
    """Площадка из начальных данных."""
    site = db.scalar(select(Site).where(Site.name == name))
    assert site is not None, f"нет площадки {name}"
    return site


def make_article(
    db: Session,
    *,
    name: str = "Zerkalo_60x160",
    group: str = "Полотна",
    subgroup: str | None = "полотна",
    sort_order: int = 1,
) -> Article:
    """Создать артикул."""
    from app.articles import normalize_article, prefix_for

    article = Article(
        kind="article",
        name=name,
        name_norm=normalize_article(name),
        group=group,
        subgroup=subgroup,
        prefix=prefix_for(name),
        color_hex="#FFE0C1",
        active=True,
        sort_order=sort_order,
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def get_csrf(client: TestClient, url: str = "/login") -> str:
    """Взять CSRF-токен со страницы."""
    response = client.get(url)
    match = CSRF_RE.search(response.text)
    assert match, f"на странице {url} нет csrf-токена"
    return match.group(1)


def login(client: TestClient, user: User, pin: str = "1234") -> None:
    """Войти в систему под сотрудником."""
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"user_id": str(user.id), "pin": pin, "next": "/", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
