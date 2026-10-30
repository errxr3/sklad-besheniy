"""Меню по ролям (SPEC §4). Пункт показывается, если роль есть в списке."""

from dataclasses import dataclass

from app.roles import ADMIN_ONLY, ALL_LOGGED_IN, LEADS, LOADERS, PRINTERS, RETURNS
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_STOREKEEPER


@dataclass(frozen=True)
class MenuItem:
    """Пункт меню."""

    title: str
    url: str
    roles: tuple[str, ...]


MENU: tuple[MenuItem, ...] = (
    MenuItem("Мои задачи", "/", (ROLE_ASSEMBLER,)),
    MenuItem("Смена", "/", (ROLE_LOADER,)),
    MenuItem("Погрузка", "/loading", LOADERS),
    MenuItem("Перемещение", "/transfer", LOADERS),
    MenuItem("Принять перемещение", "/transfer/in", LOADERS),
    MenuItem("Возвраты", "/returns", RETURNS),
    MenuItem("Этикетки без ШК", "/returns/print", RETURNS),
    MenuItem("Табло смены", "/board", LEADS),
    MenuItem("Печать этикеток", "/labels", PRINTERS),
    MenuItem("Ввод остатков", "/stock-entry", (ROLE_STOREKEEPER,)),
    MenuItem("Лишние этикетки", "/stock-entry/remove", (ROLE_STOREKEEPER,)),
    MenuItem("Задачи", "/tasks", LEADS),
    MenuItem("Найти зеркало", "/mirror", LEADS),
    MenuItem("Склад", "/warehouse", LEADS),
    MenuItem("Журнал", "/journal", LEADS),
    MenuItem("Отчёты", "/reports", LEADS),
)

@dataclass(frozen=True)
class AdminSection:
    """Раздел кабинета администратора: действия одной роли."""

    key: str
    title: str
    links: tuple[tuple[str, str, str], ...]  # (адрес, название, пояснение)


# Все действия системы, разложенные по ролям — кнопки верхнего меню администратора
ADMIN_SECTIONS: tuple[AdminSection, ...] = (
    AdminSection("lead", "Начальник смены", (
        ("/board", "Табло смены", "Кто на смене, принято и отгружено"),
        ("/tasks", "Задачи", "Задачи сборщикам на день"),
        ("/labels", "Печать этикеток", "Этикетки на задачи сборщиков, любая площадка"),
        ("/stock", "Остатки", "Как в гугл-таблице, CSV и XLSX"),
        ("/shipments", "Отгрузка", "Остатки, задачи и отгрузки по каналам за день, XLSX"),
        ("/mirror", "Найти зеркало", "По номеру этикетки: история, перепечатка"),
        ("/warehouse/in-transit", "Застряли в пути", "Перемещения, которые не приняли больше суток"),
        ("/warehouse/printed-old", "Этикетки без зеркал", "Напечатаны больше суток назад, зеркал нет"),
        ("/stock-entry/remove", "Лишние этикетки", "Скан ненаклеенных этикеток после «Собрал всё»"),
        ("/journal", "Журнал", "Все движения зеркал, CSV"),
        ("/reports/timesheet", "Табель", "Смены и часы"),
        ("/reports/plan-fact", "Задачи: план и факт", "Сделано против плана"),
    )),
    AdminSection("keeper", "Кладовщица", (
        ("/labels", "Печать этикеток", "Этикетки каждому сборщику в коробку с комплектующими"),
        ("/stock-entry", "Ввод остатков", "Этикетки сразу на склад, лишние — убрать сканом"),
        ("/stock-entry/remove", "Лишние этикетки", "Скан ненаклеенных: после ввода остатков и «Собрал всё»"),
    )),
    AdminSection("loader", "Грузчик", (
        ("/loading", "Погрузка", "Канал (Ozon, WB, СГТ, КГТ, СДЭК) → скан → закрыть"),
        ("/transfer", "Перемещение", "Отправить зеркала на другую площадку"),
        ("/transfer/in", "Принять перемещение", "Принять приехавшие зеркала"),
    )),
    AdminSection("returns", "Возвраты", (
        ("/returns", "Возвраты", "Скан — зеркало на склад Коминтерна"),
        ("/returns/print", "Этикетки без ШК", "Этикетка для вернувшегося зеркала без нашей этикетки"),
    )),
    AdminSection("admin", "Администратор", (
        ("/admin/employees", "Сотрудники", "Создать, роль, площадка, пароль"),
        ("/admin/sites", "Площадки", "Название, какие артикулы делают"),
        ("/admin/articles", "Артикулы", "Импорт TOP.xlsx, цвета, активность"),
        ("/admin/settings", "Параметры", "Размер этикетки"),
        ("/inventory", "Инвентаризация", "Пересчёт склада и исправление"),
        ("/writeoff", "Списание", "Брак, бой, потеря"),
        ("/ship-cancel", "Отмена отгрузки", "Вернуть ошибочно отгруженное"),
        ("/reports/output", "Выработка", "Сделано по сборщикам, брак"),
        ("/status", "Состояние системы", "База, гугл-таблица, бэкапы"),
    )),
    AdminSection("later", "Интеграции", (
        ("/admin/api-keys", "Ключи API", "Ключ для BLAGO OTGRUZKI"),
    )),
)

ADMIN_SECTION_BY_KEY: dict[str, AdminSection] = {section.key: section for section in ADMIN_SECTIONS}


def admin_section_url(key: str) -> str:
    """Адрес раздела кабинета администратора."""
    return "/admin" if key == "admin" else f"/admin/section/{key}"


__all__ = ["ADMIN_SECTIONS", "ALL_LOGGED_IN", "MENU", "MenuItem", "menu_for"]


INVENTORY_ITEM = MenuItem("Инвентаризация", "/inventory", (ROLE_LOADER,))


def menu_for(role: str | None, *, inventory: bool = False) -> list[MenuItem]:
    """Пункты меню, доступные роли. `inventory` — грузчику открыта инвентаризация на сегодня."""
    if not role:
        return []
    if role == ROLE_ADMIN:
        # У админа меню — разделы по ролям, внутри раздела плитки с действиями
        return [
            MenuItem(section.title, admin_section_url(section.key), ADMIN_ONLY)
            for section in ADMIN_SECTIONS
        ]
    items = [item for item in MENU if role in item.roles]
    if inventory and role in INVENTORY_ITEM.roles:
        items.append(INVENTORY_ITEM)
    return items
