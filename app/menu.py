"""Меню по ролям (SPEC §4). Пункт показывается, если роль есть в списке."""

from dataclasses import dataclass

from app.roles import ADMIN_ONLY, ALL_LOGGED_IN, LEADS, NOT_LOADER
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER


@dataclass(frozen=True)
class MenuItem:
    """Пункт меню."""

    title: str
    url: str
    roles: tuple[str, ...]


MENU: tuple[MenuItem, ...] = (
    MenuItem("Мой день", "/", (ROLE_ASSEMBLER, ROLE_LOADER)),
    MenuItem("Погрузка", "/loading", (ROLE_LOADER, *LEADS)),
    MenuItem("Табло смены", "/board", LEADS),
    MenuItem("Сделал зеркало", "/make", (*LEADS, ROLE_ASSEMBLER)),
    MenuItem("Задачи", "/tasks", LEADS),
    MenuItem("Остатки", "/stock", NOT_LOADER),
    MenuItem("Зеркало", "/mirror", NOT_LOADER),
    MenuItem("Склад", "/warehouse", LEADS),
    MenuItem("Журнал", "/journal", LEADS),
    MenuItem("Отчёты", "/reports", LEADS),
    MenuItem("Настройки", "/admin", ADMIN_ONLY),
)

__all__ = ["ALL_LOGGED_IN", "MENU", "MenuItem", "menu_for"]


def menu_for(role: str | None) -> list[MenuItem]:
    """Пункты меню, доступные роли."""
    if not role:
        return []
    return [item for item in MENU if role in item.roles]
