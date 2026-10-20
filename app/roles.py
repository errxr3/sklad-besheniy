"""Таблица «маршрут → роли». Единственный источник правды о доступе (SPEC §4, §12 п. 10)."""

from typing import Final

from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER

# Доступно без входа
PUBLIC: Final[tuple[str, ...]] = ("*public*",)

# Доступно по ключу API в заголовке X-API-Key (SPEC §10)
API_KEY: Final[tuple[str, ...]] = ("*api-key*",)

ALL_LOGGED_IN: Final[tuple[str, ...]] = (
    ROLE_ADMIN,
    ROLE_SHIFT_LEAD,
    ROLE_STOREKEEPER,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
)
LEADS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD)
ADMIN_ONLY: Final[tuple[str, ...]] = (ROLE_ADMIN,)
# Печать этикеток: кладовщик печатает заранее по задачам; сборщик не печатает,
# он только сканирует готовое зеркало (решение владельца 2026-10-20)
PRINTERS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER)
# Приёмка сканом после изготовления
ACCEPTORS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD, ROLE_ASSEMBLER)
# Просмотр остатков и карточки зеркала
VIEWERS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER)
WORKERS: Final[tuple[str, ...]] = (ROLE_ASSEMBLER, ROLE_LOADER)

# (метод, путь) -> роли, которым маршрут разрешён.
# Любой маршрут, которого здесь нет, закрыт для всех (тест следит за полнотой таблицы).
ROUTE_ROLES: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("GET", "/health"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("POST", "/login"): PUBLIC,
    ("POST", "/logout"): ALL_LOGGED_IN,
    ("GET", "/"): ALL_LOGGED_IN,
    # Настройки администратора
    ("GET", "/admin"): ADMIN_ONLY,
    ("GET", "/admin/employees"): ADMIN_ONLY,
    ("POST", "/admin/employees/create"): ADMIN_ONLY,
    ("POST", "/admin/employees/update"): ADMIN_ONLY,
    ("POST", "/admin/employees/reset-pin"): ADMIN_ONLY,
    ("GET", "/admin/sites"): ADMIN_ONLY,
    ("POST", "/admin/sites/create"): ADMIN_ONLY,
    ("POST", "/admin/sites/update"): ADMIN_ONLY,
    ("GET", "/admin/articles"): ADMIN_ONLY,
    ("POST", "/admin/articles/import"): ADMIN_ONLY,
    ("POST", "/admin/articles/toggle"): ADMIN_ONLY,
    ("POST", "/admin/articles/color"): ADMIN_ONLY,
    ("GET", "/admin/settings"): ADMIN_ONLY,
    ("POST", "/admin/settings"): ADMIN_ONLY,
    # Изготовление и печать этикеток
    ("GET", "/make"): PRINTERS,
    ("POST", "/make/print"): PRINTERS,
    ("GET", "/make/reprint/{number}"): PRINTERS,
    ("POST", "/scan/accept"): ACCEPTORS,
    ("GET", "/api/articles"): PRINTERS,
    ("GET", "/warehouse/printed-old"): PRINTERS,
    # Печать по задачам сборщиков и уборка висящих этикеток
    ("GET", "/labels"): PRINTERS,
    ("POST", "/labels/print"): PRINTERS,
    ("POST", "/labels/void"): PRINTERS,
    ("POST", "/labels/void-unit"): PRINTERS,
    # Карточка зеркала
    ("GET", "/mirror"): VIEWERS,
    ("GET", "/mirror/{number}"): VIEWERS,
    ("POST", "/mirror/{number}/assembler"): LEADS,
    ("POST", "/mirror/{number}/writeoff"): LEADS,
    # Смены
    ("GET", "/shift/start"): ALL_LOGGED_IN,
    ("POST", "/shift/start"): ALL_LOGGED_IN,
    ("GET", "/shift/end"): ALL_LOGGED_IN,
    ("POST", "/shift/end"): ALL_LOGGED_IN,
    ("GET", "/board"): LEADS,
    ("GET", "/board/data"): LEADS,
    ("POST", "/board/close-shift"): LEADS,
    ("GET", "/warehouse/open-shifts"): LEADS,
    ("GET", "/my-day/data"): ALL_LOGGED_IN,
    ("POST", "/admin/sites/{site_id}/rotate-code"): ADMIN_ONLY,
    ("GET", "/admin/sites/{site_id}/code.pdf"): ADMIN_ONLY,
    # Задачи и отчёты
    ("GET", "/tasks"): LEADS,
    ("POST", "/tasks/create"): LEADS,
    ("POST", "/tasks/update"): LEADS,
    ("POST", "/tasks/delete"): LEADS,
    ("POST", "/tasks/copy"): LEADS,
    ("POST", "/tasks/done"): ALL_LOGGED_IN,
    ("GET", "/reports"): LEADS,
    ("GET", "/reports/timesheet"): LEADS,
    ("GET", "/reports/timesheet.csv"): LEADS,
    ("GET", "/reports/plan-fact"): LEADS,
    # Погрузка на паллету (грузчик)
    ("GET", "/loading"): (*LEADS, ROLE_LOADER),
    ("POST", "/loading/new-pallet"): (*LEADS, ROLE_LOADER),
    ("POST", "/loading/scan"): (*LEADS, ROLE_LOADER),
    ("POST", "/loading/close"): (*LEADS, ROLE_LOADER),
    ("GET", "/loading/pallet/{pallet_id}"): (*LEADS, ROLE_LOADER),
    # Складские операции начальника смены
    ("GET", "/warehouse"): LEADS,
    ("GET", "/transfer"): LEADS,
    ("POST", "/transfer/scan"): LEADS,
    ("GET", "/transfer/in"): LEADS,
    ("POST", "/transfer/in/scan"): LEADS,
    ("GET", "/warehouse/in-transit"): LEADS,
    ("GET", "/writeoff"): LEADS,
    ("POST", "/writeoff/scan"): LEADS,
    ("GET", "/returns"): LEADS,
    ("POST", "/returns/scan"): LEADS,
    ("GET", "/ship-cancel"): LEADS,
    ("POST", "/ship-cancel/scan"): LEADS,
    ("GET", "/inventory"): LEADS,
    ("POST", "/inventory/start"): LEADS,
    ("GET", "/inventory/{inventory_id}"): LEADS,
    ("POST", "/inventory/{inventory_id}/scan"): LEADS,
    ("POST", "/inventory/{inventory_id}/finish"): LEADS,
    # Журнал и отчёты
    ("GET", "/journal"): LEADS,
    ("GET", "/journal.csv"): LEADS,
    ("GET", "/reports/output"): LEADS,
    # Остатки и выгрузки
    ("GET", "/stock"): VIEWERS,
    ("GET", "/stock.csv"): VIEWERS,
    ("GET", "/stock.xlsx"): VIEWERS,
    ("POST", "/stock/sync"): LEADS,
    # API для BLAGO OTGRUZKI (ключ в заголовке X-API-Key)
    ("GET", "/api/v1/stock"): API_KEY,
    ("POST", "/api/v1/shipments"): API_KEY,
    ("GET", "/api/v1/ping"): API_KEY,
    # Ключи API, состояние системы и документация API
    ("GET", "/admin/api-keys"): ADMIN_ONLY,
    ("POST", "/admin/api-keys/create"): ADMIN_ONLY,
    ("POST", "/admin/api-keys/deactivate"): ADMIN_ONLY,
    ("GET", "/status"): LEADS,
    ("GET", "/docs"): ADMIN_ONLY,
    ("GET", "/openapi.json"): ADMIN_ONLY,
}

# Маршруты, требующие открытой смены у сборщика и грузчика (SPEC §4)
NEEDS_OPEN_SHIFT: Final[set[tuple[str, str]]] = {
    ("POST", "/make/print"),
    ("POST", "/scan/accept"),
    ("POST", "/loading/new-pallet"),
    ("POST", "/loading/scan"),
}


def roles_for(method: str, path: str) -> tuple[str, ...] | None:
    """Роли, которым разрешён маршрут, или None, если маршрут не описан."""
    return ROUTE_ROLES.get((method.upper(), path))
