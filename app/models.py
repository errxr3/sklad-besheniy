"""Модели данных (SPEC §8).

Важно: статус зеркала меняется только через app.transitions.apply_transition,
которая в одной транзакции пишет движение в `movements`.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import UNIT_NUMBER_SEQUENCE, Base
from app.statuses import ALL_MOVEMENT_TYPES, ALL_ROLES, ALL_STATUSES

unit_number_seq = Sequence(UNIT_NUMBER_SEQUENCE, start=1, increment=1, metadata=Base.metadata)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Текст CHECK-ограничения «значение из списка»."""
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Site(Base):
    """Площадка: место хранения и производства."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Код смены (SPEC §5.1): храним только хеш секрета
    shift_code_hash: Mapped[str | None] = mapped_column(String(128))
    shift_code_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Геопроверка (SPEC §5.3), по умолчанию выключена
    geo_lat: Mapped[float | None] = mapped_column(Float)
    geo_lon: Mapped[float | None] = mapped_column(Float)
    geo_radius_m: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    geo_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Маркировка остатка при первом запуске (SPEC §6.11)
    labeling_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Какие артикулы делают на площадке: начала названий через запятую («Zerkalo, Alum»).
    # Пусто — любые. Ограничивает выбор артикула при постановке задач.
    article_prefixes: Mapped[str | None] = mapped_column(Text)

    @property
    def prefixes(self) -> list[str]:
        """Начала названий артикулов площадки."""
        return [part.strip() for part in (self.article_prefixes or "").split(",") if part.strip()]

    def makes(self, article_name: str) -> bool:
        """Делают ли на площадке этот артикул."""
        names = self.prefixes
        if not names:
            return True
        lowered = article_name.lower()
        return any(lowered.startswith(prefix.lower()) for prefix in names)

    def __repr__(self) -> str:
        return f"<Site {self.id} {self.name}>"


class User(Base):
    """Сотрудник. Один сотрудник = одна роль (SPEC §4)."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in_list("role", ALL_ROLES), name="ck_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))  # «Имя Фамилия», как показывается везде
    first_name: Mapped[str | None] = mapped_column(String(60))
    last_name: Mapped[str | None] = mapped_column(String(60))
    login: Mapped[str | None] = mapped_column(String(40))  # у служебных нет, войти под ними нельзя
    role: Mapped[str] = mapped_column(String(20))
    pin_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    home_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    is_service: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Удалённый сотрудник: не входит и не виден в списках, но его записи в журнале остаются
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    home_site: Mapped[Site | None] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<User {self.id} {self.name} {self.role}>"


class Article(Base):
    """Строка справочника из TOP.xlsx.

    kind='article' — артикул, kind='header' — строка-заголовок подгруппы.
    Заголовки хранятся здесь же, чтобы экран «Остатки» выводил строки
    ровно в порядке файла TOP.xlsx (SPEC §6.9).
    """

    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("kind IN ('article', 'header')", name="ck_articles_kind"),
        CheckConstraint(
            "(kind = 'header' AND name_norm IS NULL) OR (kind = 'article' AND name_norm IS NOT NULL)",
            name="ck_articles_norm_for_article",
        ),
        UniqueConstraint("name_norm", name="uq_articles_name_norm"),
        Index("ix_articles_sort_order", "sort_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), default="article", server_default="'article'")
    name: Mapped[str] = mapped_column(String(200))
    name_norm: Mapped[str | None] = mapped_column(String(200))
    group: Mapped[str] = mapped_column("group", String(40))
    subgroup: Mapped[str | None] = mapped_column(String(120))
    prefix: Mapped[str | None] = mapped_column(String(4))
    color_hex: Mapped[str | None] = mapped_column(String(7))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def is_header(self) -> bool:
        """Это строка-заголовок подгруппы, а не артикул."""
        return self.kind == "header"

    def __repr__(self) -> str:
        return f"<Article {self.id} {self.name}>"


class Pallet(Base):
    """Канал погрузки площадки (SPEC §6.4): общий для всех грузчиков на смене там."""

    __tablename__ = "pallets"
    __table_args__ = (
        UniqueConstraint("number", name="uq_pallets_number"),
        # Один открытый канал каждого вида на площадке: грузят в него все грузчики площадки.
        # У Ozon машина выбирается при открытии (note): Озон1, Озон2, Озон3 — разные каналы
        Index(
            "uq_pallets_one_open_per_channel",
            "site_id",
            "channel",
            text("coalesce(note, '')"),
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    channel: Mapped[str] = mapped_column(String(20))
    opened_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Описание при закрытии канала (для Ozon — машина: Озон1…Озон3)
    note: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(lazy="joined")
    opener: Mapped[User] = relationship(lazy="joined")

    @property
    def title(self) -> str:
        """Название канала для экрана: «Озон 2», «WB»."""
        if self.channel == "Ozon" and self.note and self.note.startswith("Озон"):
            return "Озон " + self.note.split(",")[0].removeprefix("Озон").strip()
        return self.channel

    def __repr__(self) -> str:
        return f"<Pallet {self.number}>"


class Shift(Base):
    """Смена сотрудника (SPEC §5)."""

    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('user', 'auto', 'manual')",
            name="ck_shifts_end",
        ),
        Index(
            "uq_shifts_one_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_shifts_user_started", "user_id", "started_at"),
        Index("ix_shifts_site_started", "site_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(10))
    start_ip: Mapped[str | None] = mapped_column(String(64))
    start_ua: Mapped[str | None] = mapped_column(String(300))
    start_lat: Mapped[float | None] = mapped_column(Float)
    start_lon: Mapped[float | None] = mapped_column(Float)
    edited_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    edit_note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="joined", foreign_keys=[user_id])
    site: Mapped[Site] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<Shift {self.id} user={self.user_id}>"


class Inventory(Base):
    """Инвентаризация площадки (SPEC §6.6)."""

    __tablename__ = "inventories"
    __table_args__ = (
        Index(
            "uq_inventories_one_active_per_site",
            "site_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # NULL — вся площадка; иначе список id артикулов
    article_ids: Mapped[list[int] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(lazy="joined")


class Unit(Base):
    """Зеркало — один физический экземпляр с уникальным номером."""

    __tablename__ = "units"
    __table_args__ = (
        CheckConstraint(_in_list("status", ALL_STATUSES), name="ck_units_status"),
        UniqueConstraint("number", name="uq_units_number"),
        UniqueConstraint("mp_barcode", name="uq_units_mp_barcode"),
        Index("ix_units_stock", "status", "site_id", "article_id"),
        Index("ix_units_assembler_made", "assembler_id", "made_at"),
        Index("ix_units_pallet", "pallet_id"),
        Index("ix_units_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number: Mapped[str] = mapped_column(String(12))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    status: Mapped[str] = mapped_column(String(16))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    assembler_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    made_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str | None] = mapped_column(String(20))
    pallet_id: Mapped[int | None] = mapped_column(ForeignKey("pallets.id"))
    mp_barcode: Mapped[str | None] = mapped_column(String(64))
    # Задача, под которую кладовщик напечатал этикетку (NULL — напечатано вне задач)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    article: Mapped[Article] = relationship(lazy="joined")
    site: Mapped[Site] = relationship(lazy="joined")
    assembler: Mapped[User | None] = relationship(lazy="joined", foreign_keys=[assembler_id])
    pallet: Mapped[Pallet | None] = relationship(lazy="joined")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    def __repr__(self) -> str:
        return f"<Unit {self.number} {self.status}>"


class Movement(Base):
    """Запись журнала. Не редактируется и не удаляется (триггер в БД)."""

    __tablename__ = "movements"
    __table_args__ = (
        CheckConstraint(_in_list("type", ALL_MOVEMENT_TYPES), name="ck_movements_type"),
        Index("ix_movements_unit", "unit_id", "created_at"),
        Index("ix_movements_created", "created_at"),
        Index("ix_movements_user", "user_id", "created_at"),
        Index("ix_movements_type", "type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    type: Mapped[str] = mapped_column(String(20))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    from_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    to_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(Integer)

    unit: Mapped[Unit] = relationship(lazy="select")
    user: Mapped[User | None] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<Movement {self.id} {self.type}>"


class InventoryScan(Base):
    """Скан в рамках инвентаризации (SPEC §6.6)."""

    __tablename__ = "inventory_scans"
    __table_args__ = (UniqueConstraint("inventory_id", "unit_id", name="uq_inventory_scans_unit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_id: Mapped[int] = mapped_column(ForeignKey("inventories.id"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    """Задача на день (SPEC §6.1–6.2)."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("kind IN ('article', 'text')", name="ck_tasks_kind"),
        CheckConstraint(
            "(kind = 'article' AND article_id IS NOT NULL AND qty_plan > 0)"
            " OR (kind = 'text' AND text IS NOT NULL)",
            name="ck_tasks_payload",
        ),
        Index("ix_tasks_date_site", "date", "site_id"),
        Index("ix_tasks_date_user", "date", "user_id"),
        Index(
            "uq_tasks_article_per_day",
            "date",
            "user_id",
            "article_id",
            unique=True,
            postgresql_where=text("kind = 'article'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(10))
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"))
    qty_plan: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    done_manually_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    article: Mapped[Article | None] = relationship(lazy="joined")
    user: Mapped[User] = relationship(lazy="joined", foreign_keys=[user_id])
    site: Mapped[Site] = relationship(lazy="joined")


class TaskLog(Base):
    """Журнал изменений задач (SPEC §6.2)."""

    __tablename__ = "task_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MpShipment(Base):
    """Отправление из BLAGO OTGRUZKI (SPEC §10)."""

    __tablename__ = "mp_shipments"
    __table_args__ = (
        UniqueConstraint("mp_barcode", name="uq_mp_shipments_barcode"),
        Index("ix_mp_shipments_date", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mp_barcode: Mapped[str] = mapped_column(String(64))
    article_name_norm: Mapped[str] = mapped_column(String(200))
    article_name: Mapped[str | None] = mapped_column(String(200))
    # nullable указан явно: имя поля `date` затеняет тип `date` в аннотации
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    """Настройки, меняемые из интерфейса (размер этикетки, этапы внедрения)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminLog(Base):
    """Журнал администрирования: сотрудники, площадки, коды смены, настройки."""

    __tablename__ = "admin_log"
    __table_args__ = (Index("ix_admin_log_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(60))
    object_type: Mapped[str | None] = mapped_column(String(40))
    object_id: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User | None] = relationship(lazy="joined")


class ApiKey(Base):
    """Ключ API для BLAGO OTGRUZKI (SPEC §10). Храним только хеш."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
