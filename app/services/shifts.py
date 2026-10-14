"""Смены: код площадки, начало и окончание по скану, автозакрытие, табель (SPEC §5)."""

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Movement, Shift, Site, User
from app.statuses import END_AUTO, END_MANUAL, END_USER, ROLE_ASSEMBLER, ROLE_LOADER
from app.timeutil import MOSCOW, msk_day_bounds, now_utc, to_moscow

CODE_PREFIX = "SHIFT"
# Во сколько по Москве автоматически закрываются забытые смены (SPEC §5.4)
AUTO_CLOSE_HOUR = 0
AUTO_CLOSE_MINUTE = 5


class ShiftError(Exception):
    """Ошибка работы со сменой, текст показывается пользователю."""


def hash_secret(secret: str) -> str:
    """Хеш секрета кода смены (в базе хранится только он)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def make_shift_code(session: Session, site: Site) -> str:
    """Выдать площадке новый код смены и вернуть строку для QR."""
    secret = secrets.token_urlsafe(32)
    site.shift_code_hash = hash_secret(secret)
    site.shift_code_rotated_at = now_utc()
    session.flush()
    return f"{CODE_PREFIX}:{site.id}:{secret}"


def parse_shift_code(raw: str) -> tuple[int, str]:
    """Разобрать строку из QR. Бросает ShiftError, если формат не тот."""
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) != 3 or parts[0].upper() != CODE_PREFIX:
        raise ShiftError("Код не распознан")
    try:
        site_id = int(parts[1])
    except ValueError as exc:
        raise ShiftError("Код не распознан") from exc
    return site_id, parts[2]


def check_code(session: Session, raw: str) -> Site:
    """Проверить код смены и вернуть площадку."""
    site_id, secret = parse_shift_code(raw)
    site = session.get(Site, site_id)
    if site is None:
        raise ShiftError("Код не распознан")
    if not site.active:
        raise ShiftError(f"Площадка «{site.name}» отключена")
    if not site.shift_code_hash:
        raise ShiftError("Для площадки не выпущен код смены — обратитесь к администратору")
    if not hmac.compare_digest(site.shift_code_hash, hash_secret(secret)):
        raise ShiftError("Код устарел — обратитесь к администратору")
    return site


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками на земле в метрах."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def check_geo(site: Site, lat: float | None, lon: float | None) -> None:
    """Проверить, что сотрудник в радиусе площадки (если проверка включена)."""
    if not site.geo_check_enabled:
        return
    if site.geo_lat is None or site.geo_lon is None:
        raise ShiftError("У площадки не заданы координаты — обратитесь к администратору")
    if lat is None or lon is None:
        raise ShiftError("Нужно разрешить доступ к геопозиции")
    distance = distance_m(lat, lon, site.geo_lat, site.geo_lon)
    if distance > site.geo_radius_m:
        raise ShiftError(
            f"Вы в {int(distance)} м от площадки «{site.name}», "
            f"начать смену можно в радиусе {site.geo_radius_m} м"
        )


def open_shift(session: Session, user_id: int) -> Shift | None:
    """Открытая смена сотрудника."""
    return session.scalars(
        select(Shift).where(Shift.user_id == user_id, Shift.ended_at.is_(None)).limit(1)
    ).first()


def start_shift(
    session: Session,
    *,
    user: User,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> Shift:
    """Начать смену по коду, отсканированному камерой в цеху (SPEC §5.2)."""
    existing = open_shift(session, user.id)
    if existing is not None:
        started = to_moscow(existing.started_at).strftime("%H:%M")
        raise ShiftError(f"Смена уже начата в {started}")

    site = check_code(session, code)
    check_geo(site, lat, lon)

    shift = Shift(
        user_id=user.id,
        site_id=site.id,
        started_at=now_utc(),
        start_ip=ip,
        start_ua=(user_agent or "")[:300] or None,
        start_lat=lat,
        start_lon=lon,
    )
    session.add(shift)
    session.flush()
    return shift


def end_shift(session: Session, *, user: User, code: str) -> Shift:
    """Закончить смену — тоже по скану кода площадки (SPEC §5.4)."""
    shift = open_shift(session, user.id)
    if shift is None:
        raise ShiftError("Открытой смены нет")
    site = check_code(session, code)
    if site.id != shift.site_id:
        raise ShiftError(f"Это код площадки «{site.name}», а смена открыта на другой")
    shift.ended_at = now_utc()
    shift.end_reason = END_USER
    session.flush()
    return shift


def last_scan_at(session: Session, shift_id: int) -> datetime | None:
    """Время последнего скана сотрудника в эту смену."""
    return session.scalar(
        select(func.max(Movement.created_at)).where(Movement.shift_id == shift_id)
    )


def auto_close_shifts(session: Session, moment: datetime | None = None) -> int:
    """Закрыть смены, забытые с прошлых суток (SPEC §5.4).

    Время окончания — время последнего скана в смену, иначе время начала.
    """
    moment = moment or now_utc()
    day_start, _ = msk_day_bounds(to_moscow(moment).date())
    shifts = session.scalars(
        select(Shift).where(Shift.ended_at.is_(None), Shift.started_at < day_start)
    ).all()
    for shift in shifts:
        shift.ended_at = last_scan_at(session, shift.id) or shift.started_at
        shift.end_reason = END_AUTO
    session.flush()
    return len(shifts)


def edit_shift(
    session: Session,
    *,
    shift_id: int,
    actor: User,
    ended_at: datetime | None,
    note: str,
    started_at: datetime | None = None,
) -> Shift:
    """Правка смены начальником смены: обязателен комментарий (SPEC §5.4)."""
    if not note.strip():
        raise ShiftError("Укажите причину правки")
    shift = session.get(Shift, shift_id)
    if shift is None:
        raise ShiftError("Смена не найдена")
    before = (
        f"начало {to_moscow(shift.started_at).strftime('%d.%m.%Y %H:%M')}, "
        f"окончание {to_moscow(shift.ended_at).strftime('%d.%m.%Y %H:%M') if shift.ended_at else '—'}"
    )
    if started_at is not None:
        shift.started_at = started_at
    if ended_at is not None:
        shift.ended_at = ended_at
    if shift.ended_at is not None and shift.ended_at < shift.started_at:
        raise ShiftError("Окончание раньше начала")
    shift.end_reason = END_MANUAL
    shift.edited_by = actor.id
    shift.edit_note = f"было: {before}. Причина: {note.strip()}"
    session.flush()
    return shift


@dataclass
class BoardRow:
    """Строка табло «Кто на смене»."""

    user: User
    shift: Shift | None
    accepted: int
    shipped: int
    tasks_done: int
    tasks_plan: int

    @property
    def state(self) -> str:
        """на смене / смена закончена / не вышел."""
        if self.shift is None:
            return "не вышел"
        return "на смене" if self.shift.ended_at is None else "смена закончена"


def board_rows(session: Session, site_id: int, day: date) -> list[BoardRow]:
    """Табло смены: кто вышел, сколько принято и отгружено, прогресс задач (SPEC §5.5)."""
    from app.services.tasks import tasks_progress

    start, end = msk_day_bounds(day)
    users = session.scalars(
        select(User)
        .where(User.active.is_(True), User.role.in_((ROLE_ASSEMBLER, ROLE_LOADER)))
        .order_by(User.name)
    ).all()

    shifts = {
        shift.user_id: shift
        for shift in session.scalars(
            select(Shift)
            .where(Shift.site_id == site_id, Shift.started_at >= start, Shift.started_at < end)
            .order_by(Shift.started_at)
        ).all()
    }

    counts: dict[tuple[int | None, str], int] = {
        (user_id, movement_type): int(count)
        for user_id, movement_type, count in session.execute(
            select(Movement.user_id, Movement.type, func.count())
            .where(Movement.created_at >= start, Movement.created_at < end)
            .group_by(Movement.user_id, Movement.type)
        ).all()
    }

    rows: list[BoardRow] = []
    for user in users:
        shift = shifts.get(user.id)
        if shift is None and user.home_site_id not in (None, site_id):
            continue
        progress = tasks_progress(session, user_id=user.id, day=day)
        rows.append(
            BoardRow(
                user=user,
                shift=shift,
                accepted=int(counts.get((user.id, "accept"), 0)),
                shipped=int(counts.get((user.id, "ship"), 0)),
                tasks_done=progress.done,
                tasks_plan=progress.plan,
            )
        )
    return rows


@dataclass
class TimesheetRow:
    """Строка табеля."""

    day: date
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None

    @property
    def hours(self) -> float:
        """Длительность смены в часах."""
        if self.ended_at is None:
            return 0.0
        return round((self.ended_at - self.started_at).total_seconds() / 3600, 2)

    @property
    def closed_label(self) -> str:
        """Как закрыта смена."""
        return {
            END_USER: "сотрудником",
            END_AUTO: "автоматически",
            END_MANUAL: "вручную",
        }.get(self.end_reason or "", "не закрыта")


def timesheet(
    session: Session, *, user_id: int, date_from: date, date_to: date
) -> list[TimesheetRow]:
    """Табель сотрудника за период (SPEC §5.6)."""
    start, _ = msk_day_bounds(date_from)
    _, end = msk_day_bounds(date_to)
    shifts = session.scalars(
        select(Shift)
        .where(Shift.user_id == user_id, Shift.started_at >= start, Shift.started_at < end)
        .order_by(Shift.started_at)
    ).all()
    return [
        TimesheetRow(
            day=to_moscow(shift.started_at).date(),
            started_at=shift.started_at,
            ended_at=shift.ended_at,
            end_reason=shift.end_reason,
        )
        for shift in shifts
    ]


def next_auto_close(moment: datetime | None = None) -> datetime:
    """Когда в следующий раз сработает автозакрытие (для планировщика)."""
    moment = to_moscow(moment or now_utc())
    target = moment.replace(hour=AUTO_CLOSE_HOUR, minute=AUTO_CLOSE_MINUTE, second=0, microsecond=0)
    if target <= moment:
        target = target + timedelta(days=1)
    return target.astimezone(MOSCOW)
