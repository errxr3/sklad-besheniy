"""Таблица «маршрут → роли». Единственный источник правды о доступе (SPEC §4, §12 п. 10).

Права по ролям — по решению владельца 2026-10-20 (DECISIONS.md):
- начальник смены: табло, печать по задачам, задачи, остатки, поиск зеркала,
  «в пути больше суток» и «напечатано больше суток», журнал, табель, план/факт;
- кладовщик: только печать по задачам своей площадки;
- сборщик: только сканер этикеток (+ своя смена);
- грузчик: погрузка по каналам и перемещение между площадками (+ своя смена);
- возвраты: приёмка возвратов и печать ШК для зеркал без нашей маркировки;
- администратор: всё.
"""

from typing import Final

from app.statuses import (
    ROLE_ADMIN,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_RETURNS,
    ROLE_SHIFT_LEAD,
    ROLE_STOREKEEPER,
)

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
    ROLE_RETURNS,
)
ADMIN_ONLY: Final[tuple[str, ...]] = (ROLE_ADMIN,)
LEADS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD)
# Печать этикеток по задачам сборщиков
PRINTERS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER)
# Ввод остатков (инвентаризация на старте)
STOCK_ENTRY: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_STOREKEEPER)
# Приёмка сканом готового зеркала
ACCEPTORS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_ASSEMBLER)
# Погрузка по каналам и перемещение между площадками
LOADERS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_LOADER)
# Возвраты от покупателей и печать ШК для зеркал без маркировки
RETURNS: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_RETURNS)
# Смена по QR нужна тем, кто работает в цеху
SHIFT_USERS: Final[tuple[str, ...]] = ALL_LOGGED_IN
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
    ("GET", "/admin/section/{key}"): ADMIN_ONLY,
    ("GET", "/admin/employees"): ADMIN_ONLY,
    ("POST", "/admin/employees/create"): ADMIN_ONLY,
    ("POST", "/admin/employees/update"): ADMIN_ONLY,
    ("POST", "/admin/employees/reset-pin"): ADMIN_ONLY,
    ("POST", "/admin/employees/delete"): ADMIN_ONLY,
    ("GET", "/admin/sites"): ADMIN_ONLY,
    ("POST", "/admin/sites/create"): ADMIN_ONLY,
    ("POST", "/admin/sites/update"): ADMIN_ONLY,
    ("GET", "/admin/articles"): ADMIN_ONLY,
    ("POST", "/admin/articles/import"): ADMIN_ONLY,
    ("POST", "/admin/articles/toggle"): ADMIN_ONLY,
    ("POST", "/admin/articles/color"): ADMIN_ONLY,
    ("GET", "/admin/settings"): ADMIN_ONLY,
    ("POST", "/admin/settings"): ADMIN_ONLY,
    ("POST", "/admin/sites/{site_id}/rotate-code"): ADMIN_ONLY,
    ("GET", "/admin/sites/{site_id}/code.pdf"): ADMIN_ONLY,
    # Приёмка сканом и перепечатка этикетки
    ("POST", "/scan/accept"): ACCEPTORS,
    ("GET", "/scanner"): ADMIN_ONLY,
    ("GET", "/make/reprint/{number}"): LEADS,
    ("GET", "/api/articles"): (*LEADS, ROLE_RETURNS, ROLE_STOREKEEPER),
    # Печать по задачам сборщиков и уборка висящих этикеток
    ("GET", "/labels"): PRINTERS,
    ("POST", "/labels/print"): PRINTERS,
    ("POST", "/labels/void"): PRINTERS,
    ("GET", "/warehouse/printed-old"): LEADS,
    # Ввод остатков
    ("GET", "/stock-entry"): STOCK_ENTRY,
    ("POST", "/stock-entry/print"): STOCK_ENTRY,
    ("GET", "/stock-entry/remove"): (*STOCK_ENTRY, ROLE_SHIFT_LEAD),
    ("POST", "/stock-entry/remove/scan"): (*STOCK_ENTRY, ROLE_SHIFT_LEAD),
    # «Собрал всё»: сборщик сдаёт задачу кнопкой, без скана
    ("POST", "/tasks/submit"): ACCEPTORS,
    ("POST", "/labels/void-unit"): LEADS,
    # Карточка зеркала
    ("GET", "/mirror"): LEADS,
    ("GET", "/mirror/{number}"): LEADS,
    ("POST", "/mirror/{number}/assembler"): LEADS,
    ("POST", "/mirror/{number}/writeoff"): ADMIN_ONLY,
    # Смены
    ("GET", "/shift/start"): SHIFT_USERS,
    ("POST", "/shift/start"): SHIFT_USERS,
    ("GET", "/shift/end"): SHIFT_USERS,
    ("POST", "/shift/end"): SHIFT_USERS,
    ("GET", "/board"): LEADS,
    ("GET", "/board/data"): LEADS,
    ("POST", "/board/close-shift"): LEADS,
    ("GET", "/warehouse/open-shifts"): LEADS,
    ("GET", "/my-day/data"): ALL_LOGGED_IN,
    # Задачи и отчёты
    ("GET", "/tasks"): LEADS,
    ("GET", "/tasks/import"): LEADS,
    ("POST", "/tasks/import/preview"): LEADS,
    ("POST", "/tasks/import/apply"): LEADS,
    ("POST", "/tasks/create"): LEADS,
    ("POST", "/tasks/update"): LEADS,
    ("POST", "/tasks/delete"): LEADS,
    ("POST", "/tasks/done"): ALL_LOGGED_IN,
    ("GET", "/reports"): LEADS,
    ("GET", "/reports/timesheet"): LEADS,
    ("GET", "/reports/timesheet.csv"): LEADS,
    ("GET", "/reports/plan-fact"): LEADS,
    ("GET", "/reports/output"): ADMIN_ONLY,
    # Погрузка по каналам (грузчик)
    ("GET", "/loading"): LOADERS,
    ("POST", "/loading/new-pallet"): LOADERS,
    ("POST", "/loading/scan"): LOADERS,
    ("POST", "/loading/close"): LOADERS,
    ("GET", "/loading/pallet/{pallet_id}"): (*LOADERS, ROLE_SHIFT_LEAD),
    ("GET", "/loading/batch/{pallet_id}"): (*LOADERS, ROLE_SHIFT_LEAD),
    # Перемещение между площадками (грузчик отправляет и принимает)
    ("GET", "/transfer"): LOADERS,
    ("POST", "/transfer/scan"): LOADERS,
    ("GET", "/transfer/in"): LOADERS,
    ("POST", "/transfer/in/scan"): LOADERS,
    # Возвраты
    ("GET", "/returns"): RETURNS,
    ("POST", "/returns/scan"): RETURNS,
    ("GET", "/returns/print"): RETURNS,
    ("POST", "/returns/print"): RETURNS,
    # Складские операции: начальнику смены — только два списка
    ("GET", "/warehouse"): LEADS,
    ("GET", "/warehouse/in-transit"): LEADS,
    ("GET", "/writeoff"): ADMIN_ONLY,
    ("POST", "/writeoff/scan"): ADMIN_ONLY,
    ("GET", "/ship-cancel"): ADMIN_ONLY,
    ("POST", "/ship-cancel/scan"): ADMIN_ONLY,
    ("GET", "/inventory"): ADMIN_ONLY,
    ("POST", "/inventory/start"): ADMIN_ONLY,
    ("GET", "/inventory/{inventory_id}"): ADMIN_ONLY,
    ("POST", "/inventory/{inventory_id}/scan"): ADMIN_ONLY,
    ("POST", "/inventory/{inventory_id}/finish"): ADMIN_ONLY,
    # Журнал
    ("GET", "/shipments/plan"): LEADS,
    ("GET", "/shipments/plan.xlsx"): LEADS,
    ("GET", "/shipments/batches"): LEADS,
    ("POST", "/shipments/batches/remove"): LEADS,
    ("POST", "/shipments/batches/return"): LEADS,
    ("GET", "/ship/questions"): LEADS,
    ("POST", "/ship/questions/{question_id}/answer"): LEADS,
    ("POST", "/ship/questions/answer-all"): LEADS,
    ("GET", "/journal"): LEADS,
    ("GET", "/journal.csv"): LEADS,
    # Остатки и выгрузки
    ("GET", "/stock"): LEADS,
    ("GET", "/shipments"): LEADS,
    ("GET", "/shipments.xlsx"): LEADS,
    ("GET", "/stock.csv"): LEADS,
    ("GET", "/stock.xlsx"): LEADS,
    ("POST", "/stock/sync"): LEADS,
    # API для BLAGO OTGRUZKI (ключ в заголовке X-API-Key)
    ("GET", "/api/v1/stock"): API_KEY,
    ("POST", "/api/v1/shipments"): API_KEY,
    ("GET", "/api/v1/ping"): API_KEY,
    ("POST", "/api/v1/demand"): API_KEY,
    ("POST", "/api/v1/batches"): API_KEY,
    # Ключи API, состояние системы и документация API
    ("GET", "/admin/api-keys"): ADMIN_ONLY,
    ("POST", "/admin/api-keys/create"): ADMIN_ONLY,
    ("POST", "/admin/api-keys/deactivate"): ADMIN_ONLY,
    ("GET", "/status"): ADMIN_ONLY,
    ("GET", "/docs"): ADMIN_ONLY,
    ("GET", "/openapi.json"): ADMIN_ONLY,
}

# Маршруты, требующие открытой смены у сборщика и грузчика (SPEC §4)
NEEDS_OPEN_SHIFT: Final[set[tuple[str, str]]] = {
    ("POST", "/scan/accept"),
    ("POST", "/tasks/submit"),
    ("POST", "/loading/new-pallet"),
    ("POST", "/loading/scan"),
    ("POST", "/transfer/scan"),
    ("POST", "/transfer/in/scan"),
}


def roles_for(method: str, path: str) -> tuple[str, ...] | None:
    """Роли, которым разрешён маршрут, или None, если маршрут не описан."""
    return ROUTE_ROLES.get((method.upper(), path))


def can(role: str | None, path: str, method: str = "GET") -> bool:
    """Есть ли у роли доступ к маршруту — для показа ссылок в шаблонах."""
    roles = roles_for(method, path)
    return bool(role) and roles is not None and role in roles
