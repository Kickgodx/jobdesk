"""Таблицы схемы ``hh``. Структура совпадает с тем, что оставляет миграция ядра 054."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, MetaData, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

HH_SCHEMA = "hh"


class Base(DeclarativeBase):
    metadata = MetaData(schema=HH_SCHEMA)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HhListingRow(Base):
    """Кэш короткой выдачи HH: last_seen + флаг detail_fetched для anti-DDOS."""

    __tablename__ = "hh_listings"

    hh_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    employer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    area: Mapped[str | None] = mapped_column(String(256), nullable=True)
    alternate_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    salary_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    snippet_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class HhSyncJobRow(Base):
    """Фоновый HH sync: durable-джоба с lease и recover (``platform/durable-jobs``)."""

    __tablename__ = "hh_sync_jobs"
    __table_args__ = (
        # Один активный sync: partial UNIQUE по выражению ((true)).
        Index(
            "uq_hh_sync_jobs_one_active",
            text("(true)"),
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    # Окно поиска (window / period / date_from / date_to) — снимок запроса.
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Итог sync (HhSyncResult) — и частичный, если sync упал на середине.
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # id пользователя ядра, запустившего sync; без FK — схема hh не ссылается на ядро.
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
