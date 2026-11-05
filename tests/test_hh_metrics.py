"""Метрики HH-сервиса: прежние имена серий, гейджи схемы hh — коллектором сервиса."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from tests.conftest import clear_settings_caches

from jobdesk_hh import observability
from jobdesk_hh.db.models import HhListingRow, HhSyncJobRow
from jobdesk_hh.db.session import reset_database, session_scope


@pytest.fixture
def hh_db(pg_db):
    clear_settings_caches()
    reset_database()
    yield
    reset_database()
    clear_settings_caches()


def _samples(collector) -> dict[tuple[str, tuple], float]:
    return {(s.name, tuple(sorted(s.labels.items()))): s.value for fam in collector.collect() for s in fam.samples}


def test_labelled_counters_have_zero_series_before_first_event():
    for name, labels in (("hh_syncs_total", {"outcome": "success"}), ("hh_details_total", {"result": "fetched"})):
        assert REGISTRY.get_sample_value(name, labels) is not None, (name, labels)


def test_collector_counts_listings_and_expired_sync_leases(hh_db):
    now = datetime.now(UTC)
    with session_scope() as s:
        s.add(HhListingRow(hh_id="1", name="A", content_hash="a", detail_fetched_at=now))
        s.add(HhListingRow(hh_id="2", name="B", content_hash="b"))
        s.add(HhSyncJobRow(status="completed", params={}))
    samples = _samples(observability.HhDbStatsCollector(cache_seconds=0))
    assert samples[("db_hh_listings", (("state", "total"),))] == 2
    assert samples[("db_hh_listings", (("state", "with_detail"),))] == 1
    assert samples[("db_hh_listings", (("state", "without_detail"),))] == 1
    # Серия с нулём есть: алерт не путает «нет зависших» с «нет метрики».
    assert samples[("db_jobs_lease_expired", (("kind", "hh_sync"),))] == 0


@pytest.mark.parametrize("lease_until", [None, timedelta(minutes=-5)])
def test_running_sync_without_live_lease_is_counted(hh_db, lease_until):
    with session_scope() as s:
        s.add(
            HhSyncJobRow(
                status="running",
                params={},
                lease_until=None if lease_until is None else datetime.now(UTC) + lease_until,
            )
        )
    samples = _samples(observability.HhDbStatsCollector(cache_seconds=0))
    assert samples[("db_jobs_lease_expired", (("kind", "hh_sync"),))] == 1


def test_collector_without_database_yields_nothing(monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(observability.HhDbStatsCollector, "_read_db", staticmethod(broken))
    assert list(observability.HhDbStatsCollector(cache_seconds=0).collect()) == []
