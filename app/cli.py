"""Команды управления: python -m app.cli <команда>."""

import argparse
import getpass
import sys
import time

from sqlalchemy import select, text

from app.db import get_engine, session_scope
from app.importer import import_top
from app.models import Site, User
from app.security import validate_pin_format
from app.seed import apply_seed
from app.services.admin import AdminError, create_user
from app.services.verify import verify_stock
from app.statuses import ROLE_ADMIN


def cmd_wait_for_db(args: argparse.Namespace) -> int:
    """Ждать доступности базы (используется при старте контейнера)."""
    deadline = time.monotonic() + args.timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            print("База доступна")
            return 0
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    print(f"База недоступна за {args.timeout} с: {last_error}", file=sys.stderr)
    return 1


def cmd_seed(args: argparse.Namespace) -> int:
    """Создать площадки, служебного сборщика и настройки по умолчанию, если их нет."""
    with session_scope() as session:
        apply_seed(session)
    print("Начальные данные на месте")
    return 0


def _ask_pin() -> str:
    """Спросить PIN скрытым вводом.

    Цифры намеренно не отображаются на экране, поэтому об этом предупреждаем:
    иначе кажется, что клавиатура не работает.
    """
    print("PIN вводится скрытно — цифры на экране не появятся.")
    print("Наберите 4–6 цифр и нажмите Enter.")
    try:
        return getpass.getpass("PIN (4–6 цифр): ").strip()
    except Exception:
        # Терминал без скрытого ввода — спрашиваем обычным способом
        return input("PIN (4–6 цифр, будет виден): ").strip()


def cmd_create_admin(args: argparse.Namespace) -> int:
    """Создать администратора (первый вход в систему)."""
    name = args.name or input("Имя администратора: ").strip()
    pin = args.pin or _ask_pin()
    try:
        validate_pin_format(pin)
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    with session_scope() as session:
        apply_seed(session)
        site_id = None
        if args.site:
            site = session.scalar(select(Site).where(Site.name == args.site))
            if site is None:
                print(f"Площадка «{args.site}» не найдена", file=sys.stderr)
                return 2
            site_id = site.id
        try:
            user = create_user(
                session, actor_id=None, name=name, role=ROLE_ADMIN, pin=pin, home_site_id=site_id
            )
        except AdminError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
        print(f"Администратор «{user.name}» создан")
    return 0


def cmd_import_top(args: argparse.Namespace) -> int:
    """Импортировать справочник артикулов из TOP.xlsx."""
    with session_scope() as session:
        report = import_top(session, args.path)
    print(report.as_text())
    for warning in report.warnings[:20]:
        print(f"  ! {warning}")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    """Показать сотрудников."""
    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.name)).all()
        for user in users:
            state = "активен" if user.active else "отключён"
            service = " (служебный)" if user.is_service else ""
            print(f"{user.id:>4}  {user.name:<30} {user.role:<12} {state}{service}")
    return 0


def cmd_verify_stock(args: argparse.Namespace) -> int:
    """Пересчитать остатки по журналу движений и сравнить с таблицей зеркал."""
    with session_scope() as session:
        report = verify_stock(session)
    print(f"Проверено зеркал: {report.units_checked}")
    if report.ok:
        print("Расхождений нет")
        return 0
    print(f"Найдено расхождений: {len(report.problems)}", file=sys.stderr)
    for problem in report.problems[: args.limit]:
        print(f"  ! {problem}", file=sys.stderr)
    if len(report.problems) > args.limit:
        print(f"  ... и ещё {len(report.problems) - args.limit}", file=sys.stderr)
    return 1


def cmd_close_shifts(args: argparse.Namespace) -> int:
    """Закрыть забытые смены (то же делает планировщик в 00:05 по Москве)."""
    from app.services.shifts import auto_close_shifts

    with session_scope() as session:
        closed = auto_close_shifts(session)
    print(f"Закрыто смен: {closed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Управление системой склада")
    sub = parser.add_subparsers(dest="command", required=True)

    p_wait = sub.add_parser("wait-for-db", help="ждать доступности базы")
    p_wait.add_argument("--timeout", type=int, default=60, help="сколько секунд ждать")
    p_wait.set_defaults(func=cmd_wait_for_db)

    p_seed = sub.add_parser("seed", help="создать начальные данные (площадки, настройки)")
    p_seed.set_defaults(func=cmd_seed)

    p_admin = sub.add_parser("create-admin", help="создать администратора")
    p_admin.add_argument("--name", help="имя администратора")
    p_admin.add_argument("--pin", help="PIN 4–6 цифр (иначе спросит скрытым вводом)")
    p_admin.add_argument("--site", help="площадка по умолчанию")
    p_admin.set_defaults(func=cmd_create_admin)

    p_top = sub.add_parser("import-top", help="импорт справочника артикулов из TOP.xlsx")
    p_top.add_argument("path", help="путь к файлу TOP.xlsx")
    p_top.set_defaults(func=cmd_import_top)

    p_users = sub.add_parser("list-users", help="показать сотрудников")
    p_users.set_defaults(func=cmd_list_users)

    p_shifts = sub.add_parser("close-shifts", help="закрыть забытые смены")
    p_shifts.set_defaults(func=cmd_close_shifts)

    p_verify = sub.add_parser("verify-stock", help="сверить остатки с журналом движений")
    p_verify.add_argument("--limit", type=int, default=20, help="сколько расхождений показать")
    p_verify.set_defaults(func=cmd_verify_stock)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
