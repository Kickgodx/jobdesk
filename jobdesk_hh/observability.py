"""Метрики HH-сервиса. Имена те же, что отдавало ядро до выноса: панели и алерты не меняются.

Счётчики — в процессе, который выполняет sync (hh-worker или сервис в ``inprocess``).
Гейджи по схеме ``hh`` (``db_hh_listings``, ``db_jobs_lease_expired{kind="hh_sync"}``) —
коллектор процесса API сервиса; регистрируется вместе с сервером ``/metrics``.
"""

from __future__ import annotations

import logging
import threading
import time

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily

_LOG = logging.getLogger(__name__)

HH_SYNCS = Counter(
    "hh_syncs_total",
    "Завершённые синхронизации HH",
    labelnames=("outcome",),  # success | cancelled | api_error | not_configured | error
)

HH_SYNC_DURATION = Histogram(
    "hh_sync_duration_seconds",
    "Длительность HH sync (wall time)",
    buckets=(5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 3600.0),
)

HH_SEARCHED = Counter(
    "hh_vacancies_searched_total",
    "Вакансий получено из поиска HH",
)

HH_DETAILS = Counter(
    "hh_details_total",
    "Запросы detail к HH API",
    labelnames=("result",),  # fetched | skipped
)

HH_API_ERRORS = Counter(
    "hh_api_errors_total",
    "Ошибки HH API",
    labelnames=("status",),
)

HH_LISTINGS_PURGED = Counter(
    "hh_listings_purged_total",
    "Строк hh_listings удалено по TTL",
)


def record_hh_sync_finished(*, outcome: str, duration_seconds: float, searched: int) -> None:
    HH_SYNCS.labels(outcome=outcome or "unknown").inc()
    HH_SYNC_DURATION.observe(max(duration_seconds, 0.0))
    if searched > 0:
        HH_SEARCHED.inc(searched)


def record_hh_details(*, fetched: int, skipped: int) -> None:
    if fetched > 0:
        HH_DETAILS.labels(result="fetched").inc(fetched)
    if skipped > 0:
        HH_DETAILS.labels(result="skipped").inc(skipped)


def record_hh_api_error(*, status: str) -> None:
    HH_API_ERRORS.labels(status=status or "unknown").inc()


def record_hh_listings_purged(count: int) -> None:
    if count > 0:
        HH_LISTINGS_PURGED.inc(count)


# Нули для известных лейблов: серия, появившаяся сразу с 1, для increase()/rate() — прирост 0.
for _outcome in ("success", "cancelled", "api_error", "not_configured", "error"):
    HH_SYNCS.labels(outcome=_outcome)
for _result in ("fetched", "skipped"):
    HH_DETAILS.labels(result=_result)


_JOBS_LEASE_EXPIRED_HELP = "Фоновые задачи в running с истёкшим или пустым lease (parse | llm | hh_sync)"
_CACHE_SECONDS = 30.0


class HhDbStatsCollector:
    """COUNT по схеме hh для scrape, с кэшем на ``_CACHE_SECONDS``; БД недоступна — пустой набор."""

    def __init__(self, *, cache_seconds: float = _CACHE_SECONDS) -> None:
        self._lock = threading.Lock()
        self._cache_seconds = cache_seconds
        self._cached_at: float | None = None
        self._cached: tuple[dict[str, int], int] | None = None

    def describe(self):
        yield GaugeMetricFamily("db_hh_listings", "Кэш HH listings", labels=["state"])
        yield GaugeMetricFamily("db_jobs_lease_expired", _JOBS_LEASE_EXPIRED_HELP, labels=["kind"])

    def _read_cached(self) -> tuple[dict[str, int], int]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and self._cached_at is not None and now - self._cached_at < self._cache_seconds:
                return self._cached
            data = self._read_db()
            if self._cache_seconds > 0:
                self._cached_at, self._cached = now, data
            return data

    @staticmethod
    def _read_db() -> tuple[dict[str, int], int]:
        from sqlalchemy import func, or_, select

        from jobdesk_hh.db.models import HhListingRow, HhSyncJobRow
        from jobdesk_hh.db.session import session_scope

        with session_scope() as s:
            total = int(s.scalar(select(func.count()).select_from(HhListingRow)) or 0)
            with_detail = int(
                s.scalar(
                    select(func.count()).select_from(HhListingRow).where(HhListingRow.detail_fetched_at.is_not(None))
                )
                or 0
            )
            # running с истёкшим lease: recover такую задачу возвращает в очередь — долго
            # висящая значит, что recover не работает.
            expired = int(
                s.scalar(
                    select(func.count())
                    .select_from(HhSyncJobRow)
                    .where(
                        HhSyncJobRow.status == "running",
                        or_(HhSyncJobRow.lease_until.is_(None), HhSyncJobRow.lease_until < func.now()),
                    )
                )
                or 0
            )
        return {"total": total, "with_detail": with_detail, "without_detail": total - with_detail}, expired

    def collect(self):
        try:
            listings, expired = self._read_cached()
        except Exception as e:
            _LOG.debug("HhDbStatsCollector: БД недоступна: %s", e)
            return
        fam = GaugeMetricFamily("db_hh_listings", "Кэш HH listings", labels=["state"])
        for state in ("total", "with_detail", "without_detail"):
            fam.add_metric([state], listings[state])
        yield fam
        fam = GaugeMetricFamily("db_jobs_lease_expired", _JOBS_LEASE_EXPIRED_HELP, labels=["kind"])
        fam.add_metric(["hh_sync"], expired)
        yield fam


_collector_registered = False
_collector_lock = threading.Lock()


def register_db_stats_collector() -> None:
    """Гейджи схемы hh в ``/metrics`` — только процесс API сервиса, вместе с сервером метрик."""
    global _collector_registered
    with _collector_lock:
        if _collector_registered:
            return
        REGISTRY.register(HhDbStatsCollector())
        _collector_registered = True
