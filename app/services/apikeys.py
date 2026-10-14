"""Ключи API для внешних программ (SPEC §10). В базе хранится только хеш."""

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiKey
from app.timeutil import now_utc

KEY_PREFIX = "sklad_"


def hash_key(key: str) -> str:
    """Хеш ключа."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_key(session: Session, *, name: str, created_by: int | None) -> tuple[ApiKey, str]:
    """Создать ключ. Открытое значение возвращается один раз и больше не хранится."""
    if not name.strip():
        raise ValueError("Укажите название ключа")
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    record = ApiKey(name=name.strip(), key_hash=hash_key(plain), active=True, created_by=created_by)
    session.add(record)
    session.flush()
    return record, plain


def verify_key(session: Session, key: str | None) -> ApiKey | None:
    """Найти активный ключ по открытому значению."""
    if not key:
        return None
    digest = hash_key(key.strip())
    for record in session.scalars(select(ApiKey).where(ApiKey.active.is_(True))).all():
        if hmac.compare_digest(record.key_hash, digest):
            record.last_used_at = now_utc()
            session.flush()
            return record
    return None


def deactivate_key(session: Session, key_id: int) -> ApiKey | None:
    """Отключить ключ."""
    record = session.get(ApiKey, key_id)
    if record is None:
        return None
    record.active = False
    session.flush()
    return record


def list_keys(session: Session) -> list[ApiKey]:
    """Все ключи."""
    return list(session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all())
