"""Команды управления: python -m app.cli <команда>."""

import argparse
import sys
import time

from sqlalchemy import text

from app.db import get_engine


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


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Управление системой склада")
    sub = parser.add_subparsers(dest="command", required=True)

    p_wait = sub.add_parser("wait-for-db", help="ждать доступности базы")
    p_wait.add_argument("--timeout", type=int, default=60, help="сколько секунд ждать")
    p_wait.set_defaults(func=cmd_wait_for_db)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
