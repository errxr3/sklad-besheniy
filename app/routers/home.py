"""Главная страница: своя для каждой роли (SPEC §4)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER
from app.web import GuardedRoute, page

router = APIRouter(route_class=GuardedRoute)


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    """Сборщик и грузчик — «Мой день», начальник смены и админ — «Табло смены»."""
    user = request.state.user
    if user.role in (ROLE_ASSEMBLER, ROLE_LOADER):
        return page(request, "my_day.html", {})
    return page(request, "board.html", {})
