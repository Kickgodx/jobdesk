"""Плановая очистка HH-сервиса: кэш выдачи по ``hh.listing_ttl_days`` и старые HH sync.

Срок хранения sync — ``retention.parse_runs_ttl_days``, как у прогонов (``platform/data-maintenance``).
Выполняется при старте сервиса и затем раз в ``HH_MAINTENANCE_INTERVAL_SECONDS``; реплик
может быть несколько — проход делает та, что взяла advisory lock. Сбой только пишется в журнал.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from jobdesk.config.loader import load_config
from jobdesk_hh.db import jobs, listings
from jobdesk_hh.db.session import get_database, session_scope
from jobdesk_hh.settings import get_hh_settings

_LOG = logging.getLogger(__name__)

# Ключ pg_try_advisory_lock прохода очистки (≠ lock sync, миграций HH и ядра).
HH_MAINTENANCE_LOCK_KEY = 7_242_031_012
_BATCH = 1000
# Потолок за один проход: остаток догонит следующий.
_MAX_BATCHES = 50


@dataclass(frozen=True)
class MaintenanceStats:
    listings_purged: int = 0
    sync_jobs_purged: int = 0
    skipped: bool = False


def _purge_old_jobs(older_than: datetime) -> int:
    total = 0
    for _ in range(_MAX_BATCHES):
        with session_scope() as session:
            n = jobs.purge_old_hh_sync_jobs(session, older_than=older_than, limit=_BATCH)
        total += n
        if n < _BATCH:
            break
    return total


def run_maintenance(*, now: datetime | None = None) -> MaintenanceStats:
    """Один проход очистки; ``skipped`` — проход уже делает другая реплика."""
    now = now or datetime.now(UTC)
    cfg = load_config(get_hh_settings().config_path)
    with get_database().engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": HH_MAINTENANCE_LOCK_KEY}).scalar():
            return MaintenanceStats(skipped=True)
        try:
            listings_purged = 0
            try:
                with session_scope() as session:
                    listings_purged = listings.purge_expired_listings(session, ttl_days=cfg.hh.listing_ttl_days)
            except Exception as e:
                _LOG.warning("Очистка кэша выдачи HH не выполнена: %s", e)
            sync_jobs_purged = 0
            ttl_days = cfg.retention.parse_runs_ttl_days
            if ttl_days > 0:
                try:
                    sync_jobs_purged = _purge_old_jobs(now - timedelta(days=ttl_days))
                except Exception as e:
                    _LOG.warning("Очистка истории HH sync не выполнена: %s", e)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": HH_MAINTENANCE_LOCK_KEY})
    stats = MaintenanceStats(listings_purged=listings_purged, sync_jobs_purged=sync_jobs_purged)
    if listings_purged or sync_jobs_purged:
        _LOG.info("Очистка HH: listings=%s, sync=%s", listings_purged, sync_jobs_purged)
    return stats


def _safe_run() -> None:
    try:
        run_maintenance()
    except Exception as e:
        _LOG.warning("Очистка HH не выполнена: %s", e)


def start_maintenance_loop(*, interval_seconds: int) -> threading.Event:
    """Daemon-поток: проход сразу и затем раз в ``interval_seconds``; вернуть Event остановки."""
    stop = threading.Event()

    def _loop() -> None:
        _safe_run()
        while not stop.wait(timeout=max(60, interval_seconds)):
            _safe_run()

    threading.Thread(target=_loop, name="hh-maintenance", daemon=True).start()
    return stop
