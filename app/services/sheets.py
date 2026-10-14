"""Выгрузка остатков в гугл-таблицу (SPEC §10).

Если ключ сервисного аккаунта не настроен, задача тихо пропускается с записью в лог.
Ошибки не роняют приложение: они пишутся в настройки и показываются на странице «Состояние».
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.stock import build_stock_table, site_locative
from app.settings_store import get_value, set_value
from app.timeutil import fmt_dt, now_utc

logger = logging.getLogger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

LAST_SYNC_KEY = "sheets_last_sync"
LAST_STATUS_KEY = "sheets_last_status"
LAST_ERROR_KEY = "sheets_last_error"


@dataclass
class SyncResult:
    """Итог выгрузки."""

    ok: bool
    skipped: bool
    message: str
    rows: int = 0


def _credentials_info() -> dict[str, object] | None:
    """Данные сервисного аккаунта из файла или переменной окружения."""
    settings = get_settings()
    if settings.google_service_account_file:
        try:
            with open(settings.google_service_account_file, encoding="utf-8") as handle:
                data = json.load(handle)
            return dict(data)
        except OSError as exc:
            raise RuntimeError(f"Не читается файл ключа Google: {exc}") from exc
    if settings.google_service_account_json.strip():
        return dict(json.loads(settings.google_service_account_json))
    return None


def is_configured() -> bool:
    """Настроена ли выгрузка (есть ключ и идентификатор таблицы)."""
    settings = get_settings()
    if not settings.google_sheets_id.strip():
        return False
    return bool(
        settings.google_service_account_file.strip() or settings.google_service_account_json.strip()
    )


def last_sync(session: Session) -> tuple[str, str]:
    """Время и статус последней выгрузки."""
    return get_value(session, LAST_SYNC_KEY), get_value(session, LAST_STATUS_KEY)


def minutes_since_sync(session: Session, moment: datetime | None = None) -> int | None:
    """Сколько минут прошло с последней удачной выгрузки."""
    raw = get_value(session, LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        moment_last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    delta = (moment or now_utc()) - moment_last
    return int(delta.total_seconds() // 60)


def _write_values(values: list[list[str]], updated_text: str) -> None:
    """Записать лист одним запросом (чтобы он не был наполовину пустым)."""
    import gspread
    from google.oauth2.service_account import Credentials

    settings = get_settings()
    info = _credentials_info()
    if info is None:
        raise RuntimeError("Ключ сервисного аккаунта не задан")

    credentials = Credentials.from_service_account_info(info, scopes=list(SCOPES))
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(settings.google_sheets_id)

    title = settings.google_sheet_name
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=max(len(values) + 10, 100), cols=12)

    needed_rows = len(values) + 5
    if worksheet.row_count < needed_rows:
        worksheet.add_rows(needed_rows - worksheet.row_count)

    worksheet.clear()
    end_column = chr(ord("A") + max(0, len(values[0]) - 1)) if values else "A"
    worksheet.batch_update(
        [
            {"range": f"A1:{end_column}{len(values)}", "values": values},
            {"range": "G1", "values": [[updated_text]]},
        ]
    )


def sync_stock_to_sheets(session: Session) -> SyncResult:
    """Перезаписать лист «Остатки из системы» текущими остатками."""
    if not is_configured():
        message = "Гугл-таблица не настроена: нет ID таблицы или ключа сервисного аккаунта"
        set_value(session, LAST_STATUS_KEY, message)
        logger.info("Выгрузка пропущена", extra={"status": message})
        return SyncResult(ok=False, skipped=True, message=message)

    table = build_stock_table(session)
    header = ["Артикул"]
    header.extend(site.name for site in table.sites)
    header.extend(f"Задачи на {site_locative(site.name)}" for site in table.sites)

    from app.services.stock import sheet_rows

    values = [header, *sheet_rows(table)]
    updated_text = f"Обновлено: {fmt_dt(now_utc())}"

    try:
        _write_values(values, updated_text)
    except Exception as exc:
        message = f"Выгрузка не удалась: {exc}"
        set_value(session, LAST_STATUS_KEY, message)
        set_value(session, LAST_ERROR_KEY, message)
        logger.exception("Выгрузка в гугл-таблицу не удалась")
        return SyncResult(ok=False, skipped=False, message=message)

    set_value(session, LAST_SYNC_KEY, now_utc().isoformat())
    message = f"Выгружено строк: {len(values) - 1}"
    set_value(session, LAST_STATUS_KEY, message)
    set_value(session, LAST_ERROR_KEY, "")
    return SyncResult(ok=True, skipped=False, message=message, rows=len(values) - 1)
