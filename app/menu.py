"""Меню по ролям (SPEC §4). Пункт показывается, если роль есть в списке."""

from dataclasses import dataclass

from app.roles import ADMIN_ONLY, ALL_LOGGED_IN, LEADS, LOADERS, PRINTERS, RETURNS
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER


@dataclass(frozen=True)
class MenuItem:
    """Пункт меню."""

    title: str
    url: str
    roles: tuple[str, ...]


MENU: tuple[MenuItem, ...] = (
    MenuItem("Сканер", "/", (ROLE_ASSEMBLER,)),
    MenuItem("Смена", "/", (ROLE_LOADER,)),
    MenuItem("Погрузка", "/loading", LOADERS),
    MenuItem("Перемещение", "/transfer", LOADERS),
    MenuItem("Приёмка перемещения", "/transfer/in", LOADERS),
    MenuItem("Возвраты", "/returns", RETURNS),
    MenuItem("Печать ШК", "/returns/print", RETURNS),
    MenuItem("Табло смены", "/board", LEADS),
    MenuItem("Печать по задачам", "/labels", PRINTERS),
    MenuItem("Задачи", "/tasks", LEADS),
    MenuItem("Остатки", "/stock", LEADS),
    MenuItem("Найти зеркало", "/mirror", LEADS),
    MenuItem("Склад", "/warehouse", LEADS),
    MenuItem("Журнал", "/journal", LEADS),
    MenuItem("Отчёты", "/reports", LEADS),
    MenuItem("Кабинет", "/admin", ADMIN_ONLY),
)

__all__ = ["ALL_LOGGED_IN", "MENU", "MenuItem", "menu_for"]


def menu_for(role: str | None) -> list[MenuItem]:
    """Пункты меню, доступные роли."""
    if not role:
        return []
    if role == ROLE_ADMIN:
        # У админа всё разложено по ролям в «Кабинете» — длинное меню не нужно
        return [item for item in MENU if item.url == "/admin"]
    return [item for item in MENU if role in item.roles]
