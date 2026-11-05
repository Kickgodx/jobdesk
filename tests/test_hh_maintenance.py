"""Очистка HH-сервиса: старые HH sync по сроку хранения, кэш выдачи по TTL (platform/data-maintenance)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from tests.conftest import clear_settings_caches

from jobdesk_hh import maintenance
from jobdesk_hh.db.models import HhListingRow, HhSyncJobRow
from jobdesk_hh.db.session import get_database, reset_database, session_scope

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def hh_db(pg_db, monkeypatch, tmp_path, default_test_config):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        default_test_config.read_text(encoding="utf-8")
        + "\nhh:\n  listing_ttl_days: 40\nretention:\n  parse_runs_ttl_days: 180\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    clear_settings_caches()
    reset_database()
    yield
    reset_database()
    clear_settings_caches()


def _job(status: str, finished_days_ago: float | None) -> HhSyncJobRow:
    return HhSyncJobRow(
        status=status,
        params={},
        created_at=NOW - timedelta(days=400),
        finished_at=None if finished_days_ago is None else NOW - timedelta(days=finished_days_ago),
    )


def _count(model) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_purges_old_sync_jobs_and_expired_listings(hh_db):
    with session_scope() as s:
        s.add_all([_job("completed", 365), _job("failed", 200), _job("completed", 10)])
        s.add(_job("running", None))  # активный не трогаем, даже старый
        s.add(
            HhListingRow(hh_id="old", name="Old", content_hash="a", last_seen_at=datetime.now(UTC) - timedelta(days=50))
        )
        s.add(HhListingRow(hh_id="new", name="New", content_hash="b", last_seen_at=datetime.now(UTC)))

    stats = maintenance.run_maintenance(now=NOW)

    assert (stats.sync_jobs_purged, stats.listings_purged, stats.skipped) == (2, 1, False)
    assert _count(HhSyncJobRow) == 2
    with session_scope() as s:
        assert [r.hh_id for r in s.scalars(select(HhListingRow))] == ["new"]


def test_second_replica_skips_while_lock_is_held(hh_db):
    with get_database().engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": maintenance.HH_MAINTENANCE_LOCK_KEY})
        try:
            assert maintenance.run_maintenance(now=NOW).skipped is True
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": maintenance.HH_MAINTENANCE_LOCK_KEY})
    assert maintenance.run_maintenance(now=NOW).skipped is False
