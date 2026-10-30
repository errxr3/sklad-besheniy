"""Смены: начало и окончание кнопкой, автозакрытие, табель (SPEC §5; без QR — решение владельца)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Movement, Shift, Site, User
from app.statuses import END_AUTO, END_MANUAL, END_USER, ROLE_ASSEMBLER, ROLE_LOADER
from app.timeutil import MOSCOW, msk_day_bounds, now_utc, to_moscow

# Во сколько по Москве автоматически закрываются забытые смены (SPEC §5.4)
AUTO_CLOSE_HOUR = 0
AUTO_CLOSE_MINUTE = 5


class ShiftError(Exception):
    """Ошибка работы со сменой, текст показывается пользователю."""


def open_shift(session: Session, user_id: int) -> Shift | None:
    """Открытая смена сотрудника."""
    return session.scalars(
        select(Shift).where(Shift.user_id == user_id, Shift.ended_at.is_(None)).limit(1)
    ).first()


def shift_sites(session: Session, user: User) -> list[Site]:
    """Площадки, на которых сотрудник может начать смену.

    Грузчики меняются местами — выбирают площадку сами. Сборщик работает на своей
    площадке (из карточки сотрудника); если она не задана — тоже выбирает.
    """
    sites = list(
        session.scalars(
            select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id)
        ).all()
    )
    if user.role != ROLE_LOADER and user.home_site_id:
        own = [site for site in sites if site.id == user.home_site_id]
        if own:
            return own
    return sites


def start_shift(
    session: Session,
    *,
    user: User,
    site_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Shift:
    """Начать смену кнопкой на выбранной площадке (решение владельца 2026-10-21: без QR)."""
    existing = open_shift(session, user.id)
    if existing is not None:
        started = to_moscow(existing.started_at).strftime("%H:%M")
        raise ShiftError(f"Смена уже начата в {started}")

    allowed = {site.id: site for site in shift_sites(session, user)}
    site = allowed.get(site_id)
    if site is None:
        raise ShiftError("Выберите свою площадку")

    shift = Shift(
        user_id=user.id,
        site_id=site.id,
        started_at=now_utc(),
        start_ip=ip,
        start_ua=(user_agent or "")[:300] or None,
    )
    session.add(shift)
    session.flush()
    return shift


def end_shift(session: Session, *, user: User) -> Shift:
    """Закончить смену кнопкой."""
    shift = open_shift(session, user.id)
    if shift is None:
        raise ShiftError("Открытой смены нет")
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
        .where(
            User.active.is_(True),
            User.is_service.is_(False),
            User.role.in_((ROLE_ASSEMBLER, ROLE_LOADER)),
        )
        .order_by(User.name)
    ).all()

    # Смены за день по всем площадкам: нужно отличать «не вышел» от «работает на другой»
    todays_shifts = list(
        session.scalars(
            select(Shift)
            .where(Shift.started_at >= start, Shift.started_at < end)
            .order_by(Shift.started_at)
        ).all()
    )
    here = {shift.user_id: shift for shift in todays_shifts if shift.site_id == site_id}
    elsewhere = {shift.user_id for shift in todays_shifts if shift.site_id != site_id}

    # Движения считаем только по выбранной площадке
    counts: dict[tuple[int | None, str], int] = {
        (user_id, movement_type): int(count)
        for user_id, movement_type, count in session.execute(
            select(Movement.user_id, Movement.type, func.count())
            .where(
                Movement.created_at >= start,
                Movement.created_at < end,
                (Movement.to_site_id == site_id) | (Movement.from_site_id == site_id),
            )
            .group_by(Movement.user_id, Movement.type)
        ).all()
    }

    rows: list[BoardRow] = []
    for user in users:
        shift = here.get(user.id)
        # Сегодня работает на другой площадке или приписан к другой — здесь не показываем
        if shift is None and (user.id in elsewhere or user.home_site_id not in (None, site_id)):
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
