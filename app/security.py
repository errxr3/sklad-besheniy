"""Вход, сессии, CSRF и проверка ролей (SPEC §4)."""

import hmac
import logging
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Shift, User
from app.timeutil import now_utc

logger = logging.getLogger(__name__)

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

SESSION_USER_KEY = "user_id"
SESSION_CSRF_KEY = "csrf"
SESSION_STARTED_KEY = "login_at"


class LoginError(Exception):
    """Вход не выполнен — текст предназначен пользователю."""


def hash_pin(pin: str) -> str:
    """Хеш PIN (argon2)."""
    return _hasher.hash(pin)


def verify_pin(pin_hash: str, pin: str) -> bool:
    """Проверить PIN против хеша."""
    try:
        return _hasher.verify(pin_hash, pin)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


PASSWORD_MIN = 4
PASSWORD_MAX = 64
WRONG_LOGIN = "Неверный логин или пароль"


def validate_password(password: str) -> None:
    """Пароль: любые буквы, цифры и знаки, от 4 до 64 символов (решение владельца 2026-10-26)."""
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise LoginError(f"Пароль — от {PASSWORD_MIN} до {PASSWORD_MAX} символов")
    if password != password.strip():
        raise LoginError("Пароль не должен начинаться или заканчиваться пробелом")


def find_by_login(db: Session, login: str) -> User | None:
    """Сотрудник по логину, без учёта регистра."""
    value = login.strip().lower()
    if not value:
        return None
    return db.scalar(select(User).where(func.lower(User.login) == value))


def authenticate(db: Session, login: str, password: str) -> User:
    """Проверить логин и пароль, учитывая блокировку после неудачных попыток.

    Счётчик неудач и время блокировки хранятся в БД (SPEC §4). Про несуществующий логин
    и неверный пароль говорим одинаково, чтобы логины нельзя было подобрать.
    """
    settings = get_settings()
    user = find_by_login(db, login)
    if user is None or not user.active or user.is_service or user.deleted_at is not None:
        raise LoginError(WRONG_LOGIN)

    now = now_utc()
    if user.locked_until and user.locked_until > now:
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        raise LoginError(f"Вход заблокирован, попробуйте через {minutes} мин")

    if not verify_pin(user.pin_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= settings.pin_max_attempts:
            user.locked_until = now + timedelta(minutes=settings.pin_lock_minutes)
            user.failed_logins = 0
            db.flush()
            raise LoginError(f"{WRONG_LOGIN}. Вход заблокирован на {settings.pin_lock_minutes} мин")
        left = settings.pin_max_attempts - user.failed_logins
        db.flush()
        raise LoginError(f"{WRONG_LOGIN}. Осталось попыток: {left}")

    user.failed_logins = 0
    user.locked_until = None
    db.flush()
    return user


def login_session(request: Request, user: User) -> None:
    """Записать сотрудника в сессию и выдать CSRF-токен."""
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_STARTED_KEY] = now_utc().isoformat()
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    """Выйти из системы."""
    request.session.clear()


def session_user_id(request: Request) -> int | None:
    """Идентификатор вошедшего сотрудника или None."""
    value = request.session.get(SESSION_USER_KEY)
    return int(value) if value is not None else None


def csrf_token(request: Request) -> str:
    """Текущий CSRF-токен сессии (создаётся при первом обращении)."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return str(token)


def check_csrf(request: Request, sent: str | None) -> bool:
    """Сверить присланный токен с токеном сессии."""
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not sent:
        return False
    # compare_digest не работает со строками вне ASCII, поэтому сравниваем байты
    return hmac.compare_digest(str(expected).encode("utf-8"), str(sent).encode("utf-8"))


def open_shift_for(db: Session, user_id: int) -> Shift | None:
    """Открытая смена сотрудника или None."""
    return db.scalars(
        select(Shift).where(Shift.user_id == user_id, Shift.ended_at.is_(None)).limit(1)
    ).first()
