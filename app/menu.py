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
    MenuItem("Табло смены", "/", LEADS),
    MenuItem("Сделал зеркало", "/make", (*LEADS, ROLE_ASSEMBLER)),
    MenuItem("Зеркало", "/mirror", NOT_LOADER),
    MenuItem("Напечатано вчера", "/warehouse/printed-old", LEADS),
    MenuItem("Настройки", "/admin", ADMIN_ONLY),
)

__all__ = ["ALL_LOGGED_IN", "MENU", "MenuItem", "menu_for"]


def menu_for(role: str | None) -> list[MenuItem]:
    """Пункты меню, доступные роли."""
    if not role:
        return []
    return [item for item in MENU if role in item.roles]
