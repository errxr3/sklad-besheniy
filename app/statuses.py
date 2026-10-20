"""Статусы зеркал, типы движений, роли и каналы отгрузки — единые константы."""

from typing import Final

# --- Статусы зеркала (SPEC §3) ---
PRINTED: Final = "printed"
IN_STOCK: Final = "in_stock"
IN_TRANSIT: Final = "in_transit"
SHIPPED: Final = "shipped"
WRITTEN_OFF: Final = "written_off"

ALL_STATUSES: Final[tuple[str, ...]] = (PRINTED, IN_STOCK, IN_TRANSIT, SHIPPED, WRITTEN_OFF)

STATUS_LABELS: Final[dict[str, str]] = {
    PRINTED: "Напечатано",
    IN_STOCK: "На складе",
    IN_TRANSIT: "В пути",
    SHIPPED: "Отгружено",
    WRITTEN_OFF: "Списано",
}

# --- Типы движений (SPEC §3, §6) ---
MV_PRINT: Final = "print"
MV_ACCEPT: Final = "accept"
MV_LABEL_VOID: Final = "label_void"
MV_TRANSFER_OUT: Final = "transfer_out"
MV_TRANSFER_IN: Final = "transfer_in"
MV_SHIP: Final = "ship"
MV_SHIP_CANCEL: Final = "ship_cancel"
MV_WRITE_OFF: Final = "write_off"
MV_RETURN_OK: Final = "return_ok"
MV_RETURN_BAD: Final = "return_bad"
MV_INVENTORY_FIX: Final = "inventory_fix"
MV_ASSEMBLER_CHANGE: Final = "assembler_change"

ALL_MOVEMENT_TYPES: Final[tuple[str, ...]] = (
    MV_PRINT,
    MV_ACCEPT,
    MV_LABEL_VOID,
    MV_TRANSFER_OUT,
    MV_TRANSFER_IN,
    MV_SHIP,
    MV_SHIP_CANCEL,
    MV_WRITE_OFF,
    MV_RETURN_OK,
    MV_RETURN_BAD,
    MV_INVENTORY_FIX,
    MV_ASSEMBLER_CHANGE,
)

MOVEMENT_LABELS: Final[dict[str, str]] = {
    MV_PRINT: "Этикетка напечатана",
    MV_ACCEPT: "Приёмка после изготовления",
    MV_LABEL_VOID: "Списание этикетки",
    MV_TRANSFER_OUT: "Отправка на площадку",
    MV_TRANSFER_IN: "Приёмка перемещения",
    MV_SHIP: "Отгрузка",
    MV_SHIP_CANCEL: "Отмена отгрузки",
    MV_WRITE_OFF: "Списание",
    MV_RETURN_OK: "Возврат (годное)",
    MV_RETURN_BAD: "Возврат (негодное)",
    MV_INVENTORY_FIX: "Исправление по инвентаризации",
    MV_ASSEMBLER_CHANGE: "Смена сборщика",
}

# --- Роли (SPEC §4) ---
ROLE_ADMIN: Final = "admin"
ROLE_SHIFT_LEAD: Final = "shift_lead"
ROLE_ASSEMBLER: Final = "assembler"
ROLE_LOADER: Final = "loader"
# Кладовщик: печатает этикетки заранее по задачам сборщиков (решение владельца 2026-10-20)
ROLE_STOREKEEPER: Final = "storekeeper"
# Возвраты: приёмка возвратов и печать ШК для зеркал без нашей маркировки
ROLE_RETURNS: Final = "returns"

ALL_ROLES: Final[tuple[str, ...]] = (
    ROLE_ADMIN,
    ROLE_SHIFT_LEAD,
    ROLE_STOREKEEPER,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_RETURNS,
)

ROLE_LABELS: Final[dict[str, str]] = {
    ROLE_ADMIN: "Администратор",
    ROLE_SHIFT_LEAD: "Начальник смены",
    ROLE_ASSEMBLER: "Сборщик зеркал",
    ROLE_STOREKEEPER: "Кладовщик",
    ROLE_LOADER: "Грузчик",
    ROLE_RETURNS: "Возвраты",
}

# Роли, которым нужна открытая смена для сканирующих операций (SPEC §4)
ROLES_NEED_SHIFT: Final[tuple[str, ...]] = (ROLE_ASSEMBLER, ROLE_LOADER)

# --- Каналы отгрузки (SPEC §6.4) ---
CHANNELS: Final[tuple[str, ...]] = ("Ozon", "WB", "СГТ", "КГТ", "СДЭК")
OZON: Final = "Ozon"
# Машины Ozon: при закрытии канала Ozon грузчик выбирает, в какую машину погружено
OZON_TRUCKS: Final[tuple[str, ...]] = ("Озон1", "Озон2", "Озон3")

# --- Причины списания (SPEC §6.7) ---
WRITE_OFF_REASONS: Final[tuple[str, ...]] = (
    "брак",
    "бой",
    "потеря",
    "ошибочная этикетка",
    "другое",
)

# --- Причины окончания смены ---
END_USER: Final = "user"
END_AUTO: Final = "auto"
END_MANUAL: Final = "manual"
