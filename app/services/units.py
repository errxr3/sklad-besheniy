"""Операции с зеркалами: печать этикеток, приёмка, списание, карточка (SPEC §6.3, §6.8)."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Movement, Site, Task, Unit, User
from app.numbering import next_numbers
from app.scan import clean_code, is_own_label
from app.statuses import (
    IN_STOCK,
    MV_ACCEPT,
    MV_ASSEMBLER_CHANGE,
    MV_LABEL_VOID,
    MV_PRINT,
    MV_WRITE_OFF,
    PRINTED,
    ROLE_ADMIN,
    ROLE_SHIFT_LEAD,
    STATUS_LABELS,
    WRITTEN_OFF,
)
from app.timeutil import fmt_dt, now_utc, today_msk
from app.transitions import TransitionError, accepted_at_text, apply_transition, lock_unit

MAX_PRINT_QTY = 50


class UnitError(Exception):
    """Ошибка операции с зеркалом, текст показывается пользователю."""


@dataclass
class ScanResult:
    """Результат скана для экрана и журнала последних сканов."""

    ok: bool
    message: str
    detail: str = ""
    number: str = ""
    article: str = ""
    stock: int | None = None
    extra: dict[str, str] = field(default_factory=dict)


def stock_count(session: Session, article_id: int, site_id: int) -> int:
    """Остаток артикула на площадке: число зеркал в статусе «На складе»."""
    value = session.scalar(
        select(func.count())
        .select_from(Unit)
        .where(Unit.status == IN_STOCK, Unit.article_id == article_id, Unit.site_id == site_id)
    )
    return int(value or 0)


def create_printed_units(
    session: Session,
    *,
    article_id: int,
    qty: int,
    site_id: int,
    assembler_id: int,
    actor_id: int,
    shift_id: int | None = None,
    task_id: int | None = None,
) -> list[Unit]:
    """Выдать номера и создать зеркала в статусе «Напечатано» (SPEC §6.3).

    Номера берутся из общей последовательности в одной транзакции, поэтому
    одновременная печать у двух сотрудников не даёт повторов.
    """
    if not 1 <= qty <= MAX_PRINT_QTY:
        raise UnitError(f"Количество — от 1 до {MAX_PRINT_QTY}")

    article = session.get(Article, article_id)
    if article is None or article.kind != "article":
        raise UnitError("Артикул не найден")
    if not article.active:
        raise UnitError(f"Артикул «{article.name}» отключён")

    site = session.get(Site, site_id)
    if site is None or not site.active:
        raise UnitError("Площадка не найдена или отключена")

    assembler = session.get(User, assembler_id)
    if assembler is None or not assembler.active:
        raise UnitError("Сборщик не найден")

    numbers = next_numbers(session, article.prefix or "X", qty)
    moment = now_utc()
    units: list[Unit] = []
    for number in numbers:
        unit = Unit(
            number=number,
            article_id=article.id,
            status=PRINTED,
            site_id=site.id,
            assembler_id=assembler.id,
            made_at=moment,
            task_id=task_id,
        )
        session.add(unit)
        units.append(unit)
    session.flush()

    for unit in units:
        session.add(
            Movement(
                unit_id=unit.id,
                type=MV_PRINT,
                from_status=None,
                to_status=PRINTED,
                to_site_id=site.id,
                user_id=actor_id,
                shift_id=shift_id,
                note=f"печать этикетки, сборщик {assembler.name}",
            )
        )
    session.flush()
    return units


def future_task_error(task: Task | None) -> str:
    """Текст ошибки, если задача ещё не наступила: её зеркала засчитаются только в её день."""
    if task is None or task.date <= today_msk():
        return ""
    when = task.date.strftime("%d.%m")
    return f"Это задача на {when}: сдать её можно будет {when}"


def accept_unit(
    session: Session,
    *,
    code: str,
    actor: User,
    shift_id: int | None = None,
    site_id: int | None = None,
) -> ScanResult:
    """Приёмка после изготовления: «Напечатано» → «На складе» (SPEC §6.3)."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO", "Отсканируйте этикетку с номером")

    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)

    article_name = unit.article.name
    if unit.status == IN_STOCK:
        return ScanResult(
            False,
            "Уже на складе",
            accepted_at_text(unit) or f"на площадке {unit.site.name}",
            number=number,
            article=article_name,
        )
    if unit.status != PRINTED:
        return ScanResult(
            False,
            f"Нельзя принять: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
            f"зеркало {number}",
            number=number,
            article=article_name,
        )

    if actor.role not in (ROLE_ADMIN, ROLE_SHIFT_LEAD) and unit.assembler_id != actor.id:
        assembler = unit.assembler.name if unit.assembler else "другой сборщик"
        return ScanResult(
            False,
            "Это зеркало другого сборщика",
            f"сборщик: {assembler}",
            number=number,
            article=article_name,
        )

    early = future_task_error(session.get(Task, unit.task_id) if unit.task_id else None)
    if early:
        return ScanResult(False, "Задача ещё не началась", early, number=number, article=article_name)

    target_site = site_id or unit.site_id
    try:
        apply_transition(
            session,
            unit,
            IN_STOCK,
            movement_type=MV_ACCEPT,
            user_id=actor.id,
            shift_id=shift_id,
            to_site_id=target_site,
        )
    except TransitionError as exc:
        return ScanResult(False, str(exc), number=number, article=article_name)

    return ScanResult(
        True,
        article_name,
        f"Принято · {unit.site.name}",
        number=number,
        article=article_name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )


def write_off_unit(
    session: Session,
    *,
    code: str,
    actor: User,
    reason: str,
    note: str = "",
    shift_id: int | None = None,
) -> ScanResult:
    """Списание брака, боя, потери или испорченной этикетки (SPEC §6.7)."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO", "Отсканируйте этикетку с номером")

    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)
    article_name = unit.article.name
    if unit.status == WRITTEN_OFF:
        return ScanResult(False, "Уже списано", f"зеркало {number}", number=number, article=article_name)

    movement_type = MV_LABEL_VOID if unit.status == PRINTED else MV_WRITE_OFF
    full_note = f"{reason}{': ' + note if note.strip() else ''}"
    try:
        apply_transition(
            session,
            unit,
            WRITTEN_OFF,
            movement_type=movement_type,
            user_id=actor.id,
            shift_id=shift_id,
            note=full_note,
        )
    except TransitionError as exc:
        return ScanResult(False, str(exc), number=number, article=article_name)

    return ScanResult(
        True,
        article_name,
        f"Списано · {reason}",
        number=number,
        article=article_name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )


def find_unit(session: Session, code: str) -> Unit | None:
    """Найти зеркало по номеру этикетки или по ШК маркетплейса."""
    number = clean_code(code)
    unit = session.scalar(select(Unit).where(Unit.number == number))
    if unit is not None:
        return unit
    return session.scalar(select(Unit).where(Unit.mp_barcode == number))


def unit_history(session: Session, unit_id: int) -> list[Movement]:
    """Полная история движений зеркала."""
    return list(
        session.scalars(
            select(Movement).where(Movement.unit_id == unit_id).order_by(Movement.created_at, Movement.id)
        ).all()
    )


def change_assembler(session: Session, *, unit_id: int, assembler_id: int, actor: User) -> Unit:
    """Сменить сборщика у зеркала (только начальник смены и админ, с записью в журнал)."""
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise UnitError("Зеркало не найдено")
    assembler = session.get(User, assembler_id)
    if assembler is None or not assembler.active:
        raise UnitError("Сборщик не найден")
    before = unit.assembler.name if unit.assembler else "—"
    unit.assembler_id = assembler.id
    session.add(
        Movement(
            unit_id=unit.id,
            type=MV_ASSEMBLER_CHANGE,
            from_status=unit.status,
            to_status=unit.status,
            from_site_id=unit.site_id,
            to_site_id=unit.site_id,
            user_id=actor.id,
            note=f"сборщик изменён: {before} → {assembler.name}",
        )
    )
    session.flush()
    return unit


def printed_older_than(session: Session, hours: int = 24) -> list[Unit]:
    """Этикетки в статусе «Напечатано» старше суток (SPEC §6.3)."""
    threshold = now_utc() - timedelta(hours=hours)
    return list(
        session.scalars(
            select(Unit)
            .where(Unit.status == PRINTED, Unit.made_at < threshold)
            .order_by(Unit.made_at)
        ).all()
    )


def label_rows(units: list[Unit]) -> list[tuple[str, str, str, str, str]]:
    """Данные для печати этикеток: номер, артикул, площадка, сборщик, дата."""
    return [
        (
            unit.number,
            unit.article.name,
            unit.site.name,
            unit.assembler.name if unit.assembler else "—",
            fmt_dt(unit.made_at).split(" ")[0],
        )
        for unit in units
    ]
