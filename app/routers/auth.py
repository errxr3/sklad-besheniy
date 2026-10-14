"""Вход и выход (SPEC §4)."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import LoginError, authenticate, login_session, logout_session, session_user_id
from app.web import GuardedRoute, active_users_for_login, page

logger = logging.getLogger(__name__)
router = APIRouter(route_class=GuardedRoute)


def _safe_next(value: str | None) -> str:
    """Защита от перехода на чужой сайт после входа."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> Response:
    """Экран входа: выбор сотрудника и PIN."""
    if session_user_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return page(
        request,
        "login.html",
        {"users": active_users_for_login(), "next_url": _safe_next(next), "error": None},
    )


@router.post("/login")
def login_submit(
    request: Request,
    user_id: int = Form(...),
    pin: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
) -> Response:
    """Проверить PIN и открыть сессию."""
    try:
        user = authenticate(db, user_id, pin)
    except LoginError as exc:
        logger.warning("Неудачный вход", extra={"user_id": user_id})
        return page(
            request,
            "login.html",
            {
                "users": active_users_for_login(),
                "next_url": _safe_next(next),
                "error": str(exc),
            },
        )
    login_session(request, user)
    logger.info("Вход выполнен", extra={"user_id": user.id})
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    """Выйти из системы."""
    logout_session(request)
    return RedirectResponse("/login", status_code=303)
