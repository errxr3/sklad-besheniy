"""Логи в JSON в stdout (SPEC §11)."""

import json
import logging
import sys
from typing import Any

from app.config import get_settings


class JsonFormatter(logging.Formatter):
    """Формат записи лога — одна строка JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("error_id", "user_id", "unit_number", "path", "method", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """Настроить корневой логгер один раз."""
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel("WARNING")
