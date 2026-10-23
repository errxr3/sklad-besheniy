"""Администрирование: сотрудники, площадки, настройки (SPEC §4, §5.1)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AdminLog, Article, Site, User
from app.security import LoginError, hash_pin, validate_pin_format
from app.statuses import ALL_ROLES, ROLE_ADMIN


class AdminError(Exception):
    """Ошибка действия администратора, текст показывается пользователю."""


def log_admin(
    session: Session,
    user_id: int | None,
    action: str,
    object_type: str | None = None,
    object_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Записать действие в журнал администрирования."""
    session.add(
        AdminLog(
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            before=before,
            after=after,
        )
    )


def _check_pin(pin: str) -> None:
    """Проверить формат PIN и сообщить об ошибке языком администратора."""
    try:
        validate_pin_format(pin)
    except LoginError as exc:
        raise AdminError(str(exc)) from exc


def _user_snapshot(user: User) -> dict[str, Any]:
    """Состояние сотрудника для журнала."""
    return {
        "name": user.name,
        "role": user.role,
        "active": user.active,
        "home_site_id": user.home_site_id,
    }


def create_user(
    session: Session,
    *,
    actor_id: int | None,
    name: str,
    role: str,
    pin: str,
    home_site_id: int | None = None,
) -> User:
    """Создать сотрудника."""
    name = name.strip()
    if not name:
        raise AdminError("Укажите имя сотрудника")
    if role not in ALL_ROLES:
        raise AdminError("Неизвестная роль")
    _check_pin(pin)
    if session.scalar(select(User).where(User.name == name)) is not None:
        raise AdminError(f"Сотрудник «{name}» уже есть")

    user = User(
        name=name,
        role=role,
        pin_hash=hash_pin(pin),
        active=True,
        home_site_id=home_site_id,
    )
    session.add(user)
    session.flush()
    log_admin(session, actor_id, "создан сотрудник", "user", user.id, None, _user_snapshot(user))
    return user


def update_user(
    session: Session,
    *,
    actor_id: int | None,
    user_id: int,
    name: str | None = None,
    role: str | None = None,
    active: bool | None = None,
    home_site_id: int | None = None,
) -> User:
    """Изменить сотрудника: имя, роль, площадку, активность."""
    user = session.get(User, user_id)
    if user is None:
        raise AdminError("Сотрудник не найден")
    if user.is_service:
        raise AdminError("Служебного сотрудника менять нельзя")
    if user.deleted_at is not None:
        raise AdminError("Сотрудник удалён")
    before = _user_snapshot(user)
    if name is not None and name.strip():
        user.name = name.strip()
    if role is not None:
        if role not in ALL_ROLES:
            raise AdminError("Неизвестная роль")
        user.role = role
    if active is not None:
        user.active = active
        if active:
            user.failed_logins = 0
            user.locked_until = None
    user.home_site_id = home_site_id
    session.flush()
    log_admin(session, actor_id, "изменён сотрудник", "user", user.id, before, _user_snapshot(user))
    return user


def delete_user(session: Session, *, actor_id: int | None, user_id: int) -> User:
    """Удалить сотрудника: вход закрыт, в списках не виден, журнал и отчёты с его именем остаются."""
    user = session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise AdminError("Сотрудник не найден")
    if user.is_service:
        raise AdminError("Служебного сотрудника удалить нельзя")
    if user.id == actor_id:
        raise AdminError("Себя удалить нельзя")
    if user.role == ROLE_ADMIN:
        admins = session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == ROLE_ADMIN, User.active.is_(True), User.deleted_at.is_(None))
        )
        if (admins or 0) <= 1:
            raise AdminError("Нельзя удалить последнего администратора")
    before = _user_snapshot(user)
    user.active = False
    user.deleted_at = datetime.now(UTC)
    session.flush()
    log_admin(session, actor_id, "удалён сотрудник", "user", user.id, before, _user_snapshot(user))
    return user


def reset_pin(session: Session, *, actor_id: int | None, user_id: int, pin: str) -> User:
    """Сбросить PIN сотрудника и снять блокировку входа."""
    user = session.get(User, user_id)
    if user is None:
        raise AdminError("Сотрудник не найден")
    if user.is_service:
        raise AdminError("У служебного сотрудника нет входа")
    _check_pin(pin)
    user.pin_hash = hash_pin(pin)
    user.failed_logins = 0
    user.locked_until = None
    session.flush()
    log_admin(session, actor_id, "сброшен PIN", "user", user.id)
    return user


def create_site(session: Session, *, actor_id: int | None, name: str) -> Site:
    """Добавить площадку."""
    name = name.strip()
    if not name:
        raise AdminError("Укажите название площадки")
    if session.scalar(select(Site).where(Site.name == name)) is not None:
        raise AdminError(f"Площадка «{name}» уже есть")
    last = session.scalar(select(Site.sort_order).order_by(Site.sort_order.desc()).limit(1)) or 0
    site = Site(name=name, active=True, sort_order=last + 1)
    session.add(site)
    session.flush()
    log_admin(session, actor_id, "создана площадка", "site", site.id, None, {"name": name})
    return site


def update_site(
    session: Session,
    *,
    actor_id: int | None,
    site_id: int,
    name: str | None = None,
    active: bool | None = None,
    sort_order: int | None = None,
    geo_lat: float | None = None,
    geo_lon: float | None = None,
    geo_radius_m: int | None = None,
    geo_check_enabled: bool | None = None,
    labeling_done: bool | None = None,
    article_prefixes: str | None = None,
) -> Site:
    """Изменить площадку: название, активность, порядок, геопроверку, маркировку, артикулы."""
    site = session.get(Site, site_id)
    if site is None:
        raise AdminError("Площадка не найдена")
    before = {
        "name": site.name,
        "active": site.active,
        "sort_order": site.sort_order,
        "geo_check_enabled": site.geo_check_enabled,
        "geo_lat": site.geo_lat,
        "geo_lon": site.geo_lon,
        "geo_radius_m": site.geo_radius_m,
        "labeling_done": site.labeling_done,
        "article_prefixes": site.article_prefixes,
    }
    if name is not None and name.strip():
        site.name = name.strip()
    if active is not None:
        site.active = active
    if sort_order is not None:
        site.sort_order = sort_order
    if geo_check_enabled is not None:
        site.geo_check_enabled = geo_check_enabled
    site.geo_lat = geo_lat
    site.geo_lon = geo_lon
    if geo_radius_m is not None:
        site.geo_radius_m = max(10, geo_radius_m)
    if labeling_done is not None:
        site.labeling_done = labeling_done
    if article_prefixes is not None:
        parts = [part.strip() for part in article_prefixes.split(",") if part.strip()]
        site.article_prefixes = ", ".join(parts) or None
    session.flush()
    after = {
        "name": site.name,
        "active": site.active,
        "sort_order": site.sort_order,
        "geo_check_enabled": site.geo_check_enabled,
        "geo_lat": site.geo_lat,
        "geo_lon": site.geo_lon,
        "geo_radius_m": site.geo_radius_m,
        "labeling_done": site.labeling_done,
        "article_prefixes": site.article_prefixes,
    }
    log_admin(session, actor_id, "изменена площадка", "site", site.id, before, after)
    return site


def set_article_active(session: Session, *, actor_id: int | None, article_id: int, active: bool) -> Article:
    """Включить или выключить артикул (строка остаётся в справочнике)."""
    article = session.get(Article, article_id)
    if article is None or article.kind != "article":
        raise AdminError("Артикул не найден")
    before = {"active": article.active}
    article.active = active
    session.flush()
    log_admin(session, actor_id, "изменён артикул", "article", article.id, before, {"active": active})
    return article


def set_subgroup_color(
    session: Session, *, actor_id: int | None, group: str, subgroup: str | None, color_hex: str
) -> int:
    """Поменять цвет строк подгруппы на экране «Остатки» (SPEC §6.9)."""
    color = color_hex.strip().upper()
    if not color.startswith("#") or len(color) != 7:
        raise AdminError("Цвет задаётся в виде #RRGGBB")
    query = select(Article).where(Article.kind == "article", Article.group == group)
    query = query.where(Article.subgroup == subgroup) if subgroup else query.where(Article.subgroup.is_(None))
    articles = list(session.scalars(query).all())
    for article in articles:
        article.color_hex = color
    session.flush()
    log_admin(
        session,
        actor_id,
        "изменён цвет подгруппы",
        "subgroup",
        None,
        {"group": group, "subgroup": subgroup},
        {"color_hex": color, "строк": len(articles)},
    )
    return len(articles)
