"""Конфигурация приложения. Берётся только из переменных окружения (см. .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения из переменных окружения."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://sklad:sklad@localhost:5432/sklad"

    secret_key: str = "dev-secret-key-only-for-local-run"
    session_cookie_secure: bool = True
    session_hours: int = 14
    pin_max_attempts: int = 5
    pin_lock_minutes: int = 15

    display_tz: str = "Europe/Moscow"
    log_level: str = "INFO"

    google_sheets_id: str = ""
    google_service_account_file: str = ""
    google_service_account_json: str = ""
    google_sheet_name: str = "Остатки из системы"
    google_sync_minutes: int = 5

    scheduler_enabled: bool = True

    @property
    def is_test(self) -> bool:
        """Признак тестовой базы — по имени БД в строке подключения."""
        return self.database_url.rstrip("/").endswith("_test")


@lru_cache
def get_settings() -> Settings:
    """Настройки приложения (кешируются на процесс)."""
    return Settings()
