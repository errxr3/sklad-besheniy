"""Текущая площадка и смена сотрудника."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, Site, User
from app.security import open_shift_for


class NoSiteError(Exception):
    """Не удалось определить площадку сотрудника."""


def current_shift(session: Session, user: User) -> Shift | None:
    """Открытая смена сотрудника."""
    return open_shift_for(session, user.id)


def current_site(session: Session, user: User) -> Site:
    """Площадка, на которой сотрудник сейчас работает.

    Площадка открытой смены; если смены нет (начальник смены и админ могут
    работать без неё) — площадка сотрудника по умолчанию, иначе первая активная.
    """
    shift = current_shift(session, user)
    if shift is not None:
        site = session.get(Site, shift.site_id)
        if site is not None:
            return site
    if user.home_site_id:
        site = session.get(Site, user.home_site_id)
        if site is not None and site.active:
            return site
    site = session.scalars(
        select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id).limit(1)
    ).first()
    if site is None:
        raise NoSiteError("Нет ни одной активной площадки — обратитесь к администратору")
    return site


def site_and_shift(session: Session, user: User) -> tuple[Site, int | None]:
    """Площадка и идентификатор открытой смены (если есть)."""
    shift = current_shift(session, user)
    if shift is not None:
        site = session.get(Site, shift.site_id)
        if site is not None:
            return site, shift.id
    return current_site(session, user), None
