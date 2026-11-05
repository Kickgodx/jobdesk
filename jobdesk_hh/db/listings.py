"""Кэш короткой выдачи HH (``hh.hh_listings``) и межпроцессный lock sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from jobdesk.config.loader import load_config
from jobdesk.hh.dedup import vacancy_key_hex
from jobdesk.hh.mapper import parse_hh_datetime
from jobdesk.hh.models import VacancyShort
from jobdesk_hh.db.models import HhListingRow
from jobdesk_hh.observability import record_hh_listings_purged
from jobdesk_hh.settings import get_hh_settings

_LOG = logging.getLogger(__name__)

# Произвольный ключ pg_advisory_lock для HH sync (межпроцессный 409).
HH_SYNC_ADVISORY_LOCK_KEY = 874_291_001


@dataclass(frozen=True)
class HhListingCounts:
    total: int = 0
    with_detail: int = 0
    without_detail: int = 0


def _listing_ttl_days() -> int:
    try:
        return load_config(get_hh_settings().config_path).hh.listing_ttl_days
    except Exception:
        return 40


def _clip(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def listing_detail_fetched(session: Session, hh_id: str) -> bool:
    row = session.get(HhListingRow, hh_id)
    return row is not None and row.detail_fetched_at is not None


def mark_detail_fetched(session: Session, hh_id: str, *, when: datetime | None = None) -> None:
    row = session.get(HhListingRow, hh_id)
    if row is None:
        return
    row.detail_fetched_at = when or datetime.now(UTC)
    row.updated_at = datetime.now(UTC)


class HhSyncDbLock:
    """Межпроцессный lock HH sync: Postgres advisory на выделенном соединении.

    Advisory-lock принадлежит соединению, а sync коммитит по ходу работы: после
    commit сессия возвращает соединение в пул и дальше может получить другое.
    Lock на соединении сессии поэтому «протекал» — unlock уходил не туда, и
    следующий sync получал 409 до рестарта. Здесь lock живёт на своём соединении
    (AUTOCOMMIT — без idle-in-transaction на всё время sync) от ``try_acquire``
    до ``release``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._conn: Connection | None = None

    def try_acquire(self) -> bool:
        conn = self._session.get_bind().engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": HH_SYNC_ADVISORY_LOCK_KEY}).scalar())
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._conn = conn
        return True

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": HH_SYNC_ADVISORY_LOCK_KEY})
        except Exception as exc:
            # Вернуть в пул соединение с неснятым lock нельзя: закрываем его совсем —
            # Postgres освобождает advisory-lock вместе с сессией.
            _LOG.warning("HH advisory unlock failed, соединение закрыто: %s", exc)
            conn.invalidate()
        finally:
            conn.close()


def upsert_listings_from_shorts(
    session: Session,
    shorts: list[VacancyShort],
    *,
    now: datetime | None = None,
) -> int:
    """
    Upsert короткой выдачи.

    Если ``content_hash`` изменился — сбрасываем ``detail_fetched_at``,
    чтобы следующий sync заново скачал detail.
    """
    if not shorts:
        return 0
    now = now or datetime.now(UTC)
    count = 0
    reset_detail = 0
    for short in shorts:
        salary = short.salary_range or short.salary
        salary_json = salary.model_dump(mode="json", by_alias=True) if salary is not None else None
        snippet_json = short.snippet.model_dump(mode="json") if short.snippet is not None else None
        raw = short.model_dump(mode="json", by_alias=True)
        new_hash = vacancy_key_hex(short)
        values = {
            "hh_id": short.id,
            "name": (short.name or "")[:512],
            "employer": _clip(short.employer.name if short.employer else None, 256),
            "area": _clip(short.area.name if short.area else None, 256),
            "alternate_url": short.alternate_url,
            "published_at": parse_hh_datetime(short.published_at),
            "content_hash": new_hash,
            "salary_json": salary_json,
            "snippet_json": snippet_json,
            "raw_json": raw,
            "last_seen_at": now,
            "updated_at": now,
            "created_at": now,
        }
        existing = session.get(HhListingRow, short.id)
        if existing is None:
            session.add(HhListingRow(**values, detail_fetched_at=None))
        else:
            hash_changed = existing.content_hash != new_hash
            existing.name = values["name"]
            existing.employer = values["employer"]
            existing.area = values["area"]
            existing.alternate_url = values["alternate_url"]
            existing.published_at = values["published_at"]
            existing.content_hash = values["content_hash"]
            existing.salary_json = values["salary_json"]
            existing.snippet_json = values["snippet_json"]
            existing.raw_json = values["raw_json"]
            existing.last_seen_at = now
            existing.updated_at = now
            if hash_changed and existing.detail_fetched_at is not None:
                existing.detail_fetched_at = None
                reset_detail += 1
        count += 1
    session.flush()
    if reset_detail:
        _LOG.info(
            "HH listings: content_hash изменился у %s — detail_fetched_at сброшен",
            reset_detail,
        )
    return count


def purge_expired_listings(
    session: Session,
    *,
    now: datetime | None = None,
    ttl_days: int | None = None,
) -> int:
    ttl = ttl_days if ttl_days is not None else _listing_ttl_days()
    if ttl <= 0:
        return 0
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=ttl)
    result = session.execute(delete(HhListingRow).where(HhListingRow.last_seen_at < cutoff))
    deleted = int(result.rowcount or 0)
    if deleted:
        _LOG.info("HH listings purged: %s (ttl=%s d)", deleted, ttl)
        record_hh_listings_purged(deleted)
    return deleted


def listing_counts(session: Session) -> HhListingCounts:
    total = int(session.scalar(select(func.count()).select_from(HhListingRow)) or 0)
    with_detail = int(
        session.scalar(
            select(func.count()).select_from(HhListingRow).where(HhListingRow.detail_fetched_at.is_not(None))
        )
        or 0
    )
    return HhListingCounts(
        total=total,
        with_detail=with_detail,
        without_detail=total - with_detail,
    )
