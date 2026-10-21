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
        ("/labels", "Печать по задачам", "Этикетки на план сборщиков, любая площадка"),
        ("/stock", "Остатки", "Как в гугл-таблице, CSV и XLSX"),
        ("/shipments", "Отгрузка", "Остатки, задачи и отгрузки по каналам — копировать в таблицу"),
        ("/mirror", "Найти зеркало", "По номеру этикетки: история, перепечатка"),
        ("/warehouse/in-transit", "В пути больше суток", "Перемещения, которые не приняли"),
        ("/warehouse/printed-old", "Напечатано больше суток", "Этикетки без скана, убрать лишние"),
        ("/journal", "Журнал", "Все движения зеркал, CSV"),
        ("/reports/timesheet", "Табель", "Смены и часы"),
        ("/reports/plan-fact", "Задачи: план и факт", "Сделано против плана"),
    )),
    AdminSection("keeper", "Кладовщица", (
        ("/labels", "Печать по задачам", "Этикетки каждому сборщику в коробку с комплектующими"),
    )),
    AdminSection("assembler", "Сборщик", (
        ("/scanner", "Сканер приёмки", "Скан этикетки готового зеркала — запись на склад"),
    )),
    AdminSection("loader", "Грузчик", (
        ("/loading", "Погрузка", "Канал (Ozon, WB, СГТ, КГТ, СДЭК) → скан → закрыть"),
        ("/transfer", "Перемещение", "Отправить зеркала на другую площадку"),
        ("/transfer/in", "Приёмка перемещения", "Принять приехавшие зеркала"),
    )),
    AdminSection("returns", "Возвраты", (
        ("/returns", "Возвраты", "Скан — зеркало на склад Коминтерна"),
        ("/returns/print", "Печать ШК", "Этикетка для зеркала без нашей маркировки"),
    )),
    AdminSection("admin", "Администратор", (
        ("/admin/employees", "Сотрудники", "Создать, роль, площадка, PIN"),
        ("/admin/sites", "Площадки и QR смены", "Лист с QR-кодом, перевыпуск кода"),
        ("/admin/articles", "Артикулы", "Импорт TOP.xlsx, цвета, активность"),
        ("/admin/settings", "Параметры", "Размер этикетки"),
        ("/inventory", "Инвентаризация", "Пересчёт склада и исправление"),
        ("/writeoff", "Списание", "Брак, бой, потеря"),
        ("/ship-cancel", "Отмена отгрузки", "Вернуть ошибочно отгруженное"),
        ("/reports/output", "Выработка", "Сделано по сборщикам, брак"),
        ("/status", "Состояние системы", "База, гугл-таблица, бэкапы"),
    )),
    AdminSection("later", "Интеграции", (
        ("/admin/api-keys", "Ключи API", "Доступ программы отгрузок — подключим позже"),
    )),
)

ADMIN_SECTION_BY_KEY: dict[str, AdminSection] = {section.key: section for section in ADMIN_SECTIONS}


def admin_section_url(key: str) -> str:
    """Адрес раздела кабинета администратора."""
    return "/admin" if key == "admin" else f"/admin/section/{key}"


__all__ = ["ADMIN_SECTIONS", "ALL_LOGGED_IN", "MENU", "MenuItem", "menu_for"]


def menu_for(role: str | None) -> list[MenuItem]:
    """Пункты меню, доступные роли."""
    if not role:
        return []
    if role == ROLE_ADMIN:
        # У админа меню — разделы по ролям, внутри раздела плитки с действиями
        return [
            MenuItem(section.title, admin_section_url(section.key), ADMIN_ONLY)
            for section in ADMIN_SECTIONS
        ]
    return [item for item in MENU if role in item.roles]
