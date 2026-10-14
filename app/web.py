"""Общее для веб-слоя: шаблоны, проверка доступа, ответы на сканы."""

import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db import get_session_factory
from app.menu import menu_for
from app.models import User
from app.roles import NEEDS_OPEN_SHIFT, PUBLIC, roles_for
from app.security import check_csrf, csrf_token, open_shift_for, session_user_id
from app.statuses import (
    MOVEMENT_LABELS,
    ROLE_ADMIN,
    ROLE_ASSEMBLER,
    ROLE_LABELS,
    ROLE_LOADER,
    ROLE_SHIFT_LEAD,
    ROLES_NEED_SHIFT,
    STATUS_LABELS,
)
from app.timeutil import fmt_date, fmt_day_month, fmt_dt, fmt_time, today_msk

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    ROLE_LABELS=ROLE_LABELS,
    MOVEMENT_LABELS=MOVEMENT_LABELS,
    ROLE_ADMIN=ROLE_ADMIN,
    ROLE_SHIFT_LEAD=ROLE_SHIFT_LEAD,
    ROLE_ASSEMBLER=ROLE_ASSEMBLER,
    ROLE_LOADER=ROLE_LOADER,
)
templates.env.filters.update(
    dt=fmt_dt,
    time=fmt_time,
    date=fmt_date,
    day_month=fmt_day_month,
)


class AccessDenied(HTTPException):
    """403 с понятным текстом."""

    def __init__(self, detail: str = "Этот раздел вам недоступен") -> None:
        super().__init__(status_code=403, detail=detail)


class NeedLogin(Exception):
    """Требуется вход."""


class NeedShift(HTTPException):
    """Операция требует открытой смены (SPEC §4)."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail="Сначала начните смену")


def wants_json(request: Request) -> bool:
    """Запрос ждёт JSON (вызов из JS), а не страницу."""
    if request.url.path.startswith("/api/"):
        return True
    if request.headers.get("x-requested-with") == "fetch":
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def load_user(request: Request) -> User | None:
    """Сотрудник из сессии (отдельная короткая сессия БД)."""
    user_id = session_user_id(request)
    if user_id is None:
        return None
    with get_session_factory()() as session:
        user = session.get(User, user_id)
        if user is None or not user.active:
            return None
        session.expunge(user)
        return user


def check_access(request: Request, method: str, path: str) -> None:
    """Проверить доступ к маршруту по таблице ролей. Вызывается для каждого запроса."""
    allowed = roles_for(method, path)
    if allowed is None:
        logger.error("Маршрут без описания в таблице ролей", extra={"path": path, "method": method})
        raise AccessDenied("Маршрут не настроен")
    if allowed == PUBLIC:
        return

    user = load_user(request)
    if user is None:
        raise NeedLogin
    if user.role not in allowed:
        raise AccessDenied

    if (method, path) in NEEDS_OPEN_SHIFT and user.role in ROLES_NEED_SHIFT:
        with get_session_factory()() as session:
            if open_shift_for(session, user.id) is None:
                raise NeedShift

    request.state.user = user


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def csrf_ok(request: Request) -> bool:
    """Проверить CSRF-токен запроса: заголовок, поле формы или поле JSON."""
    token = request.headers.get("x-csrf-token")
    if not token:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get("csrf_token")
            token = value if isinstance(value, str) else None
        elif content_type.startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                raw = body.get("csrf_token")
                token = raw if isinstance(raw, str) else None
    return check_csrf(request, token)


class GuardedRoute(APIRoute):
    """Маршрут с серверной проверкой роли и CSRF до вызова обработчика."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path
        methods = sorted(self.methods or {"GET"})

        async def guarded(request: Request) -> Response:
            method = request.method.upper()
            if method not in methods and methods:
                method = methods[0]
            try:
                check_access(request, request.method.upper(), path)
            except NeedLogin:
                if wants_json(request):
                    return JSONResponse({"ok": False, "error": "Нужен вход"}, status_code=401)
                target = request.url.path
                return RedirectResponse(f"/login?next={target}", status_code=303)
            except NeedShift as exc:
                if wants_json(request):
                    return JSONResponse({"ok": False, "error": exc.detail}, status_code=403)
                return error_page(request, exc.detail, 403)
            except AccessDenied as exc:
                if wants_json(request):
                    return JSONResponse({"ok": False, "error": exc.detail}, status_code=403)
                return error_page(request, exc.detail, 403)

            if request.method.upper() in UNSAFE_METHODS and not await csrf_ok(request):
                message = "Форма устарела, обновите страницу и повторите"
                logger.warning("Запрос без CSRF-токена", extra={"path": path, "method": request.method})
                if wants_json(request):
                    return JSONResponse({"ok": False, "error": message}, status_code=403)
                return error_page(request, message, 403)

            return await original(request)

        return guarded


async def read_json(request: Request) -> dict[str, Any]:
    """Тело запроса как словарь. Пустой словарь, если тело не JSON."""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def error_page(request: Request, message: str, status_code: int) -> HTMLResponse:
    """Страница с понятной ошибкой."""
    response = templates.TemplateResponse(
        request,
        "error.html",
        {"message": message, "status_code": status_code, "user": load_user(request)},
        status_code=status_code,
    )
    return response


def page(request: Request, template: str, context: dict[str, Any] | None = None) -> HTMLResponse:
    """Отрисовать страницу с общими переменными."""
    user = getattr(request.state, "user", None) or load_user(request)
    data: dict[str, Any] = {
        "user": user,
        "menu": menu_for(user.role if user else None),
        "active_url": "/" + request.url.path.lstrip("/").split("/")[0],
        "csrf": csrf_token(request),
        "today": today_msk(),
    }
    data.update(context or {})
    return templates.TemplateResponse(request, template, data)


def active_users_for_login() -> list[User]:
    """Список активных сотрудников для экрана входа (без служебных)."""
    with get_session_factory()() as session:
        users = session.scalars(
            select(User)
            .where(User.active.is_(True), User.is_service.is_(False))
            .order_by(User.name)
        ).all()
        for user in users:
            session.expunge(user)
        return list(users)
