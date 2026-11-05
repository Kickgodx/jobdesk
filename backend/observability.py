"""Prometheus-метрики доменных событий backend.

Дополняет ``jobdesk.observability.llm_metrics`` (LLM-вызовы) и
Instrumentator (HTTP): прогоны парсинга, UPSERT вакансий, TTL-архивация,
ручной импорт, матчинг резюме↔вакансия, auth, загрузки курсов ЦБ.

Плюс ``_DBStatsCollector`` — COUNT-агрегаты по таблицам для scrape ``/metrics``
с кэшем на ``METRICS_DB_STATS_CACHE_SECONDS``. При недоступной БД коллектор
молча отдаёт пустой набор, чтобы не ломать scrape.

``_DBPoolCollector`` — состояние SQLAlchemy QueuePool (занято / свободно /
overflow / лимит). Регистрируется и на backend, и на llm-worker.

Несколько процессов API (``API_WORKERS`` > 1) пишут метрики в каталог
``PROMETHEUS_MULTIPROC_DIR`` (multiprocess-режим prometheus_client), лидер отдаёт
их сумму (``metrics_registry``). У gauge'ей поэтому задан ``multiprocess_mode``;
пул БД и GC в этом режиме публикует ``start_pool_gauges``, ``process_*`` всех
процессов (лейбл ``pid``) — ``_ApiProcessesCollector``, а файлы умерших процессов
убирает ``collect_dead_process_metrics``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

_LOG = logging.getLogger(__name__)

# --- Прогоны парсинга -------------------------------------------------------

PARSE_RUNS = Counter(
    "parse_runs_total",
    "Завершённые прогоны парсинга",
    labelnames=("status",),  # completed | partial | failed | cancelled
)

# Отказы 503 OVERLOADED (backend/overload.py). В http_requests_total{status="503"} они
# смешаны с 503 UPSTREAM_UNAVAILABLE от HH — алерт перегрузки считает по этому счётчику.
API_OVERLOAD_REJECTIONS = Counter(
    "api_overload_rejections_total",
    "Запросы, отклонённые 503 OVERLOADED: место обработки не освободилось за API_QUEUE_TIMEOUT_SECONDS",
)

PARSE_RUNS_ACTIVE = Gauge(
    "parse_runs_active",
    "Прогоны парсинга, выполняющиеся прямо сейчас",
    multiprocess_mode="livesum",
)

PARSE_RUN_STARTED_TIMESTAMP = Gauge(
    "parse_run_started_timestamp_seconds",
    "Unix-время старта активного прогона (0 если нет)",
    multiprocess_mode="livemax",
)

PARSE_RUN_DURATION = Histogram(
    "parse_run_duration_seconds",
    "Длительность прогона парсинга (wall time)",
    buckets=(10.0, 30.0, 60.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0, 7200.0, 14400.0),
)

PARSE_POSTS = Counter(
    "parse_posts_fetched_total",
    "Постов Telegram загружено прогонами",
)


def mark_parse_run_started() -> None:
    PARSE_RUN_STARTED_TIMESTAMP.set(time.time())


def mark_parse_run_stopped() -> None:
    PARSE_RUN_STARTED_TIMESTAMP.set(0.0)


def record_parse_run_finished(*, status: str, posts_fetched: int, duration_seconds: float) -> None:
    PARSE_RUNS.labels(status=status or "unknown").inc()
    PARSE_RUN_DURATION.observe(max(duration_seconds, 0.0))
    if posts_fetched > 0:
        PARSE_POSTS.inc(posts_fetched)


# --- Вакансии в БД ----------------------------------------------------------

VACANCY_UPSERTS = Counter(
    "vacancies_upserted_total",
    "Строк вакансий, записанных UPSERT'ом",
    labelnames=("result",),  # inserted | updated
)

VACANCY_ARCHIVED = Counter(
    "vacancies_archived_total",
    "Вакансий отправлено в архив по TTL",
)


# Нули для известных лейблов: иначе первый прирост серии не виден increase()/rate()
# (см. jobdesk/observability/parse_metrics.py).
_FINISHED_RUN_STATUSES = ("completed", "partial", "failed", "cancelled")
for _status in _FINISHED_RUN_STATUSES:
    PARSE_RUNS.labels(status=_status)
for _result in ("inserted", "updated"):
    VACANCY_UPSERTS.labels(result=_result)


def init_llm_model_series() -> None:
    """Нулевые LLM-серии для моделей ``config.yaml`` — на старте процесса, который ходит в LLM.

    Клиент создаётся непосредственно перед первым вызовом (в прогоне — после fetch), и
    заведённые им серии не успевают попасть в scrape до первого инкремента.
    """
    from backend.settings import get_settings
    from jobdesk.config.loader import load_config
    from jobdesk.observability.llm_metrics import init_model_series

    try:
        init_model_series(load_config(get_settings().config_path).ai.resolved_models())
    except Exception as e:
        _LOG.warning("LLM-метрики: модели из конфига не заведены: %s", e)


def record_vacancy_upsert(*, inserted: int, updated_existing: int) -> None:
    if inserted > 0:
        VACANCY_UPSERTS.labels(result="inserted").inc(inserted)
    if updated_existing > 0:
        VACANCY_UPSERTS.labels(result="updated").inc(updated_existing)


def record_vacancies_archived(count: int) -> None:
    if count > 0:
        VACANCY_ARCHIVED.inc(count)


# --- Ручной импорт ----------------------------------------------------------

IMPORT_ITEMS = Counter(
    "vacancy_import_items_total",
    "Позиции ручного импорта вакансий",
    labelnames=("status",),  # created | exists | not_vacancy | error
)


def record_import_item(status: str) -> None:
    IMPORT_ITEMS.labels(status=status or "error").inc()


# --- Матчинг резюме ↔ вакансия ----------------------------------------------

MATCH_CALLS = Counter(
    "vacancy_match_calls_total",
    "Оценки соответствия резюме вакансии",
    labelnames=("outcome",),  # success | cached | error
)

MATCH_DURATION = Histogram(
    "vacancy_match_duration_seconds",
    "Длительность LLM-матчинга (вызов + разбор)",
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
)

MATCH_SCORE = Histogram(
    "vacancy_match_score",
    "Распределение overall_score по успешным матчам",
    buckets=(5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 100.0),
)


def record_match(*, outcome: str, duration_seconds: float | None = None, score: float | None = None) -> None:
    MATCH_CALLS.labels(outcome=outcome).inc()
    if duration_seconds is not None:
        MATCH_DURATION.observe(max(duration_seconds, 0.0))
    if score is not None:
        MATCH_SCORE.observe(score)


# --- Сопроводительные письма -------------------------------------------------

COVER_LETTER_CALLS = Counter(
    "cover_letter_calls_total",
    "Генерации сопроводительных писем резюме↔вакансия",
    labelnames=("outcome",),  # success | cached | error
)

COVER_LETTER_DURATION = Histogram(
    "cover_letter_duration_seconds",
    "Длительность LLM-генерации письма (вызов + разбор)",
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
)


def record_cover_letter(*, outcome: str, duration_seconds: float | None = None) -> None:
    COVER_LETTER_CALLS.labels(outcome=outcome).inc()
    if duration_seconds is not None:
        COVER_LETTER_DURATION.observe(max(duration_seconds, 0.0))


# --- Фоновые LLM-джобы (мэтч / письмо) --------------------------------------

LLM_JOB_CALLS = Counter(
    "llm_jobs_total",
    "Фоновые LLM-задачи мэтчинга и писем",
    labelnames=("outcome", "kind"),  # queued|done|error|cancelled × match|cover_letter|import
)

LLM_JOB_DURATION = Histogram(
    "llm_job_duration_seconds",
    "Длительность фоновой LLM-задачи (очередь + выполнение)",
    labelnames=("kind",),
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
)


def record_llm_job(*, outcome: str, kind: str, duration_seconds: float | None = None) -> None:
    LLM_JOB_CALLS.labels(outcome=outcome, kind=kind or "unknown").inc()
    if duration_seconds is not None:
        LLM_JOB_DURATION.labels(kind=kind or "unknown").observe(max(duration_seconds, 0.0))


LLM_JOB_ENQUEUE = Counter(
    "llm_job_enqueue_total",
    "Постановка LLM-джоб в очередь",
    labelnames=("backend", "kind", "outcome"),  # queued|reused|rejected_limit|rejected_quota|rejected_budget|error
)

# Имя метрики историческое (была только очередь llm); лейбл queue — llm | parse.
LLM_RQ_QUEUE_JOBS = Gauge(
    "llm_rq_queue_jobs",
    "Глубина очередей RQ (llm, parse)",
    labelnames=("queue",),
    multiprocess_mode="livemax",  # очередь одна на всех — не суммировать по процессам
)

LLM_WORKER_JOBS = Counter(
    "llm_worker_jobs_total",
    "Исполнение LLM-джоб на worker",
    labelnames=("kind", "outcome"),  # started|done|error|cancelled
)

LLM_WORKER_DURATION = Histogram(
    "llm_worker_job_duration_seconds",
    "Длительность run_llm_job на worker",
    labelnames=("kind",),
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

LLM_JOBS_RECOVER = Counter(
    "llm_jobs_recover_total",
    "Recover зависших LLM-джоб при старте worker",
    labelnames=("from_status",),
)

LLM_JOB_SSE_CONNECTIONS = Gauge(
    "llm_job_sse_connections",
    "Активные SSE-подписки на статус LLM-джоб",
    multiprocess_mode="livesum",
)


def record_llm_job_enqueue(*, backend: str, kind: str, outcome: str) -> None:
    LLM_JOB_ENQUEUE.labels(
        backend=backend or "unknown",
        kind=kind or "unknown",
        outcome=outcome or "unknown",
    ).inc()


def record_llm_worker_job(*, kind: str, outcome: str, duration_seconds: float | None = None) -> None:
    LLM_WORKER_JOBS.labels(kind=kind or "unknown", outcome=outcome or "unknown").inc()
    if duration_seconds is not None:
        LLM_WORKER_DURATION.labels(kind=kind or "unknown").observe(max(duration_seconds, 0.0))


def record_llm_jobs_recover(*, from_status: str) -> None:
    LLM_JOBS_RECOVER.labels(from_status=from_status or "unknown").inc()


PARSE_JOBS_RECOVER = Counter(
    "parse_jobs_recover_total",
    "Recover зависших parse-прогонов при старте worker",
    labelnames=("from_status",),
)


def record_parse_jobs_recover(*, from_status: str) -> None:
    PARSE_JOBS_RECOVER.labels(from_status=from_status or "unknown").inc()


def set_llm_rq_queue_depth(count: int | None, *, queue: str = "llm") -> None:
    if count is None:
        return
    LLM_RQ_QUEUE_JOBS.labels(queue=queue).set(count)


# --- Auth -------------------------------------------------------------------

AUTH_EVENTS = Counter(
    "auth_events_total",
    "События аутентификации",
    labelnames=(
        "event",
        "outcome",
    ),  # register|login|refresh|change_password|logout_all|token_create|token_revoke × success|closed|conflict|invalid|disabled|rate_limited
)


def record_auth_event(event: str, outcome: str) -> None:
    AUTH_EVENTS.labels(event=event, outcome=outcome).inc()


# --- Курсы валют (ЦБ) --------------------------------------------------------

FX_CBR_REQUESTS = Counter(
    "fx_cbr_requests_total",
    "HTTP-запросы к ЦБ за курсами валют",
    labelnames=("outcome",),  # fetched | failed
)


def record_fx_fetch(outcome: str) -> None:
    FX_CBR_REQUESTS.labels(outcome=outcome).inc()


# Нули для известных лейблов (как у PARSE_RUNS выше): серия, появившаяся сразу с 1,
# для increase()/rate() — прирост 0, и первое событие после старта процесса не видно.
for _status in ("created", "exists", "not_vacancy", "error"):
    IMPORT_ITEMS.labels(status=_status)
for _outcome in ("success", "cached", "error"):
    MATCH_CALLS.labels(outcome=_outcome)
    COVER_LETTER_CALLS.labels(outcome=_outcome)
for _event, _outcomes in (
    ("register", ("success", "conflict", "closed", "rate_limited")),
    ("login", ("success", "invalid", "disabled", "rate_limited")),
    ("refresh", ("success", "invalid")),
    ("change_password", ("success", "invalid")),
    ("logout_all", ("success",)),
    ("token_create", ("success",)),
    ("token_revoke", ("success",)),
):
    for _outcome in _outcomes:
        AUTH_EVENTS.labels(event=_event, outcome=_outcome)
for _status in ("running", "queued"):
    LLM_JOBS_RECOVER.labels(from_status=_status)
    PARSE_JOBS_RECOVER.labels(from_status=_status)


def init_llm_job_series(kinds, *, backend: str | None = None) -> None:
    """Нулевые серии LLM-джоб по видам — на старте процесса, который их ставит или исполняет.

    Виды живут в ``backend.services.llm_job_service`` (импорт оттуда сюда — цикл), поэтому
    передаются вызывающим. ``backend`` (inprocess | rq) — лейбл постановки, только у API.
    """
    for kind in kinds:
        for outcome in ("queued", "done", "error", "cancelled"):
            LLM_JOB_CALLS.labels(outcome=outcome, kind=kind)
        for outcome in ("started", "done", "error", "cancelled"):
            LLM_WORKER_JOBS.labels(kind=kind, outcome=outcome)
        if backend:
            for outcome in ("queued", "reused"):
                LLM_JOB_ENQUEUE.labels(backend=backend, kind=kind, outcome=outcome)


# --- Пул соединений SQLAlchemy -----------------------------------------------


class _DBPoolCollector:
    """Состояние QueuePool на scrape ``/metrics``.

    На пуле не QueuePool (NullPool и т.п.) метрики не отдаются.
    Ошибки чтения пула глотаются: scrape не должен падать.
    """

    def describe(self):
        yield GaugeMetricFamily("db_pool_checked_out", "Соединения, выданные из пула")
        yield GaugeMetricFamily("db_pool_checked_in", "Простаивающие соединения в пуле")
        yield GaugeMetricFamily("db_pool_size", "Настроенный размер постоянного пула")
        yield GaugeMetricFamily("db_pool_overflow", "Текущий overflow сверх pool_size (≥0)")
        yield GaugeMetricFamily("db_pool_max_overflow", "Максимальный overflow (настройка)")
        yield GaugeMetricFamily(
            "db_pool_max_connections",
            "Верхняя граница соединений процесса (pool_size + max_overflow)",
        )

    def collect(self):
        stats = self._read_pool()
        if stats is None:
            return
        yield GaugeMetricFamily(
            "db_pool_checked_out",
            "Соединения, выданные из пула",
            value=stats["checked_out"],
        )
        yield GaugeMetricFamily(
            "db_pool_checked_in",
            "Простаивающие соединения в пуле",
            value=stats["checked_in"],
        )
        yield GaugeMetricFamily(
            "db_pool_size",
            "Настроенный размер постоянного пула",
            value=stats["size"],
        )
        yield GaugeMetricFamily(
            "db_pool_overflow",
            "Текущий overflow сверх pool_size (≥0)",
            value=stats["overflow"],
        )
        yield GaugeMetricFamily(
            "db_pool_max_overflow",
            "Максимальный overflow (настройка)",
            value=stats["max_overflow"],
        )
        yield GaugeMetricFamily(
            "db_pool_max_connections",
            "Верхняя граница соединений процесса (pool_size + max_overflow)",
            value=stats["max_connections"],
        )

    @staticmethod
    def _read_pool() -> dict[str, float] | None:
        try:
            from sqlalchemy.pool import QueuePool

            from backend.db.session import get_engine

            pool = get_engine().pool
            if not isinstance(pool, QueuePool):
                return None
            size = float(pool.size())
            # QueuePool._max_overflow: -1 = без лимита (редко); иначе ≥0.
            raw_max_overflow = getattr(pool, "_max_overflow", 0)
            try:
                max_overflow = float(raw_max_overflow)
            except (TypeError, ValueError):
                max_overflow = 0.0
            if max_overflow < 0:
                # unlimited overflow — capacity неизвестна; отдаём только size как floor
                max_connections = size
                max_overflow_metric = -1.0
            else:
                max_overflow_metric = max_overflow
                max_connections = size + max_overflow
            return {
                "checked_out": float(pool.checkedout()),
                "checked_in": float(pool.checkedin()),
                "size": size,
                "overflow": float(max(pool.overflow(), 0)),
                "max_overflow": max_overflow_metric,
                "max_connections": max_connections,
            }
        except Exception as e:
            _LOG.debug("DBPoolCollector: пул недоступен: %s", e)
            return None


# --- Агрегаты БД на scrape ---------------------------------------------------


# hh_sync — у HH-сервиса (jobdesk_hh.observability), то же имя метрики.
_JOBS_LEASE_EXPIRED_HELP = "Фоновые задачи в running с истёкшим или пустым lease (parse | llm | hh_sync)"


class _DBStatsCollector:
    """COUNT-запросы по таблицам для scrape /metrics, с кэшем.

    Выполняется в процессе backend. Результат живёт
    ``METRICS_DB_STATS_CACHE_SECONDS``: частые или параллельные scrape не
    повторяют ~12 COUNT'ов по всем таблицам (параллельные ждут первый под
    lock'ом). При недоступной БД возвращает пустой набор и ничего не кэширует —
    scrape не должен падать из-за postgres. ``describe()`` нужен, чтобы
    ``REGISTRY.register`` не дёргал ``collect()`` (и БД) при импорте.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._cached: tuple | None = None

    def _read_db_cached(self) -> tuple:
        from backend.settings import get_settings

        ttl = get_settings().metrics_db_stats_cache_seconds
        with self._lock:
            now = time.monotonic()
            if ttl > 0 and self._cached_at is not None and now - self._cached_at < ttl:
                return self._cached
            data = self._read_db()
            self._cached_at, self._cached = (now, data) if ttl > 0 else (None, None)
            return data

    def describe(self):
        yield GaugeMetricFamily("db_vacancies", "Вакансии в БД", labels=["state"])
        yield GaugeMetricFamily("db_resumes", "Резюме в БД")
        yield GaugeMetricFamily("db_users", "Пользователи в БД")
        yield GaugeMetricFamily("db_vacancy_matches", "Оценки резюме↔вакансия")
        yield GaugeMetricFamily("db_cover_letters", "Сопроводительные письма в БД")
        yield GaugeMetricFamily("db_ai_cache_entries", "Записи LLM-кэша в БД")
        yield GaugeMetricFamily("db_parse_runs", "Прогоны в БД по статусу", labels=["status"])
        yield GaugeMetricFamily(
            "db_parse_runs_finished_24h",
            "Прогоны с finished_at за последние 24 ч",
            labels=["status"],
        )
        yield GaugeMetricFamily(
            "db_parse_posts_fetched_24h",
            "Сумма posts_fetched у прогонов, завершённых за 24 ч",
        )
        yield GaugeMetricFamily("fx_latest_rate_timestamp_seconds", "Дата последнего курса ЦБ (unix time)")
        yield GaugeMetricFamily("llm_jobs_by_status", "LLM-джобы в БД по статусу", labels=["status"])
        yield GaugeMetricFamily("db_jobs_lease_expired", _JOBS_LEASE_EXPIRED_HELP, labels=["kind"])

    def collect(self):
        try:
            (
                counts,
                runs_by_status,
                runs_finished_24h,
                posts_fetched_24h,
                fx_latest_ts,
                llm_jobs_by_status,
                jobs_lease_expired,
            ) = self._read_db_cached()
        except Exception as e:
            _LOG.debug("DBStatsCollector: БД недоступна: %s", e)
            return
        try:
            from backend.queue.rq_conn import llm_queue_length, parse_queue_depth

            set_llm_rq_queue_depth(llm_queue_length())
            # Прогон висит в Redis, а parse-worker его не берёт: recover в rq-режиме живёт в
            # самом воркере, и без этой серии упавший worker виден только по queued в БД.
            set_llm_rq_queue_depth(parse_queue_depth(), queue="parse")
        except Exception:
            pass

        fam = GaugeMetricFamily("db_vacancies", "Вакансии в БД", labels=["state"])
        fam.add_metric(["active"], counts["vacancies_active"])
        fam.add_metric(["archived"], counts["vacancies_archived"])
        yield fam

        yield GaugeMetricFamily("db_resumes", "Резюме в БД", value=counts["resumes"])
        yield GaugeMetricFamily("db_users", "Пользователи в БД", value=counts["users"])
        yield GaugeMetricFamily(
            "db_vacancy_matches",
            "Сохранённые оценки резюме↔вакансия",
            value=counts["matches"],
        )
        yield GaugeMetricFamily(
            "db_cover_letters",
            "Сохранённые сопроводительные письма",
            value=counts["cover_letters"],
        )
        yield GaugeMetricFamily("db_ai_cache_entries", "Записи LLM-кэша в БД", value=counts["ai_cache"])

        fam = GaugeMetricFamily("db_parse_runs", "Прогоны в БД по статусу", labels=["status"])
        for status, n in runs_by_status.items():
            fam.add_metric([status], n)
        yield fam

        fam = GaugeMetricFamily(
            "db_parse_runs_finished_24h",
            "Прогоны с finished_at за последние 24 ч",
            labels=["status"],
        )
        # Нули для завершающих статусов: без серии stat-панель «Упало за 24 ч» — No data, а не 0.
        for status in sorted(set(_FINISHED_RUN_STATUSES) | set(runs_finished_24h)):
            fam.add_metric([status], runs_finished_24h.get(status, 0))
        yield fam

        yield GaugeMetricFamily(
            "db_parse_posts_fetched_24h",
            "Сумма posts_fetched у прогонов, завершённых за 24 ч",
            value=posts_fetched_24h,
        )

        if fx_latest_ts is not None:
            yield GaugeMetricFamily(
                "fx_latest_rate_timestamp_seconds",
                "Дата последнего загруженного курса ЦБ (unix time)",
                value=fx_latest_ts,
            )

        fam = GaugeMetricFamily("llm_jobs_by_status", "LLM-джобы в БД по статусу", labels=["status"])
        for status, n in llm_jobs_by_status.items():
            fam.add_metric([status], n)
        yield fam

        fam = GaugeMetricFamily("db_jobs_lease_expired", _JOBS_LEASE_EXPIRED_HELP, labels=["kind"])
        for kind, n in jobs_lease_expired.items():
            fam.add_metric([kind], n)
        yield fam

    @staticmethod
    def _read_db() -> tuple[
        dict[str, int],
        dict[str, int],
        dict[str, int],
        int,
        float | None,
        dict[str, int],
        dict[str, int],
    ]:
        from datetime import timedelta

        from sqlalchemy import func, or_, select

        from backend.db.models import (
            AIExtractionCacheRow,
            CoverLetterRow,
            ExchangeRateRow,
            LlmJobRow,
            ParseRunRow,
            ResumeRow,
            UserRow,
            VacancyMatchRow,
            VacancyRow,
        )
        from backend.db.session import session_scope

        counts: dict[str, int] = {}
        runs_by_status: dict[str, int] = {}
        runs_finished_24h: dict[str, int] = {}
        posts_fetched_24h = 0
        fx_latest_ts: float | None = None
        llm_jobs_by_status: dict[str, int] = {}
        jobs_lease_expired: dict[str, int] = {}
        since = datetime.now(UTC) - timedelta(hours=24)

        with session_scope() as s:
            counts["vacancies_active"] = int(
                s.scalar(select(func.count()).select_from(VacancyRow).where(VacancyRow.archived_at.is_(None))) or 0
            )
            counts["vacancies_archived"] = int(
                s.scalar(select(func.count()).select_from(VacancyRow).where(VacancyRow.archived_at.is_not(None))) or 0
            )
            counts["resumes"] = int(s.scalar(select(func.count()).select_from(ResumeRow)) or 0)
            counts["users"] = int(s.scalar(select(func.count()).select_from(UserRow)) or 0)
            counts["matches"] = int(s.scalar(select(func.count()).select_from(VacancyMatchRow)) or 0)
            counts["cover_letters"] = int(s.scalar(select(func.count()).select_from(CoverLetterRow)) or 0)
            counts["ai_cache"] = int(s.scalar(select(func.count()).select_from(AIExtractionCacheRow)) or 0)

            for status, n in s.execute(select(ParseRunRow.status, func.count()).group_by(ParseRunRow.status)):
                runs_by_status[str(status)] = int(n)

            for status, n in s.execute(
                select(ParseRunRow.status, func.count())
                .where(ParseRunRow.finished_at.is_not(None))
                .where(ParseRunRow.finished_at >= since)
                .group_by(ParseRunRow.status)
            ):
                runs_finished_24h[str(status)] = int(n)

            posts_fetched_24h = int(
                s.scalar(
                    select(func.coalesce(func.sum(ParseRunRow.posts_fetched), 0)).where(
                        ParseRunRow.finished_at.is_not(None),
                        ParseRunRow.finished_at >= since,
                    )
                )
                or 0
            )

            latest = s.scalar(select(func.max(ExchangeRateRow.rate_date)))
            if latest is not None:
                fx_latest_ts = datetime(latest.year, latest.month, latest.day, tzinfo=UTC).timestamp()

            for status, n in s.execute(select(LlmJobRow.status, func.count()).group_by(LlmJobRow.status)):
                llm_jobs_by_status[str(status)] = int(n)

            # running с истёкшим lease: recover (раз в JOB_RECOVER_INTERVAL_SECONDS) такую
            # задачу возвращает в очередь — долго висящая значит, что recover не работает.
            db_now = func.now()
            for kind, model in (("parse", ParseRunRow), ("llm", LlmJobRow)):
                jobs_lease_expired[kind] = int(
                    s.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(
                            model.status == "running",
                            or_(model.lease_until.is_(None), model.lease_until < db_now),
                        )
                    )
                    or 0
                )

        return (
            counts,
            runs_by_status,
            runs_finished_24h,
            posts_fetched_24h,
            fx_latest_ts,
            llm_jobs_by_status,
            jobs_lease_expired,
        )


# Пул — на каждом процессе (backend и воркеры): у каждого свой QueuePool.
REGISTRY.register(_DBPoolCollector())

_db_stats_lock = threading.Lock()
_db_stats_registered = False


def register_db_stats_collector() -> None:
    """COUNT-агрегаты таблиц в ``/metrics`` — только процесс API (``backend.main``).

    Явный вызов, а не проверка env при импорте: воркеры импортируют этот модуль
    транзитивно (recover, tasks) раньше, чем успевают выставить свою роль, и
    отдавали те же ``db_*`` — ``sum(db_users)`` на дашборде показывал ×3. Плюс
    лишние COUNT'ы по всем таблицам на каждой реплике.
    """
    global _db_stats_registered
    with _db_stats_lock:
        if not _db_stats_registered:
            REGISTRY.register(_DBStatsCollector())
            _db_stats_registered = True


# --- Несколько процессов API (multiprocess) ---------------------------------

_POOL_GAUGE_INTERVAL_S = 10.0
_POOL_GAUGE_HELP = {
    "checked_out": "Соединения, выданные из пула",
    "checked_in": "Простаивающие соединения в пуле",
    "size": "Настроенный размер постоянного пула",
    "overflow": "Текущий overflow сверх pool_size (≥0)",
    "max_overflow": "Максимальный overflow (настройка)",
    "max_connections": "Верхняя граница соединений процесса (pool_size + max_overflow)",
}


def multiprocess_dir() -> str | None:
    """Каталог multiprocess-метрик, если процессов API несколько (его задаёт ``backend.main.main``)."""
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR") or None


def metrics_registry():
    """Registry для сервера ``/metrics``: глобальный, а при нескольких процессах — их сумма.

    В multiprocess-режиме custom collectors глобального ``REGISTRY`` других процессов
    не видны: COUNT-агрегаты таблиц считает тот процесс, что отдаёт ``/metrics``.
    """
    path = multiprocess_dir()
    if path is None:
        return REGISTRY
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=path)
    registry.register(_ApiProcessesCollector(path))
    if _db_stats_registered:
        registry.register(_DBStatsCollector())
    return registry


def _pids_in_multiprocess_dir(path: str) -> set[int]:
    """pid из имён файлов каталога (``counter_<pid>.db``, ``gauge_livesum_<pid>.db``…)."""
    pids: set[int] = set()
    for name in os.listdir(path):
        if not name.endswith(".db"):
            continue
        try:
            pids.add(int(name[: -len(".db")].rsplit("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return pids


class _ApiProcessesCollector:
    """``process_*`` всех живых процессов API с лейблом ``pid``.

    ``MultiProcessCollector`` метрик процесса не отдаёт, а ``ProcessCollector`` видит только
    тот процесс, что обслуживает scrape. Процессы берутся из файлов каталога метрик; живой —
    тот, у кого есть ``/proc/<pid>`` (вне Linux ``/proc`` нет — метрик тоже).
    """

    def __init__(self, path: str, proc: str = "/proc") -> None:
        self._path = path
        self._proc = proc

    def collect(self):
        from prometheus_client import ProcessCollector
        from prometheus_client.metrics_core import Metric

        try:
            pids = _pids_in_multiprocess_dir(self._path)
        except OSError:
            pids = set()
        pids.add(os.getpid())
        merged: dict[str, Metric] = {}
        for pid in sorted(pids):
            if not os.path.isdir(os.path.join(self._proc, str(pid))):
                continue
            collector = ProcessCollector(pid=lambda pid=pid: pid, proc=self._proc, registry=None)
            for family in collector.collect():
                out = merged.setdefault(family.name, Metric(family.name, family.documentation, family.type))
                for s in family.samples:
                    out.add_sample(s.name, {**s.labels, "pid": str(pid)}, s.value)
        yield from merged.values()


def start_pool_gauges() -> threading.Event | None:
    """Пул БД и GC процесса в multiprocess-метрики раз в 10 с; без multiprocess — None.

    ``_DBPoolCollector`` читает пул на scrape, но scrape обслуживает один процесс:
    пулы остальных он не видит. Gauge'и не регистрируются в ``REGISTRY`` — там те же
    имена отдаёт коллектор; в multiprocess-режиме значения уходят в файлы каталога.
    ``python_gc_*`` — по той же причине: ``GCCollector`` в сумму процессов не попадает,
    поэтому каждый процесс докидывает прирост ``gc.get_stats()`` в свои счётчики.
    """
    if multiprocess_dir() is None:
        return None
    import gc

    gauges = {
        key: Gauge(f"db_pool_{key}", help_text, registry=None, multiprocess_mode="livesum")
        for key, help_text in _POOL_GAUGE_HELP.items()
    }
    gc_counters = {
        "collections": Counter(
            "python_gc_collections", "Сборки GC по поколениям", labelnames=("generation",), registry=None
        ),
        "collected": Counter(
            "python_gc_objects_collected", "Объектов собрано GC", labelnames=("generation",), registry=None
        ),
        "uncollectable": Counter(
            "python_gc_objects_uncollectable",
            "Несобираемых объектов, найденных GC",
            labelnames=("generation",),
            registry=None,
        ),
    }
    gc_seen: dict[tuple[str, int], int] = {}
    stop = threading.Event()

    def _tick_gc() -> None:
        for gen, stats in enumerate(gc.get_stats()):
            for key, counter in gc_counters.items():
                value = int(stats.get(key, 0))
                delta = value - gc_seen.get((key, gen), 0)
                gc_seen[(key, gen)] = value
                counter.labels(generation=str(gen)).inc(max(delta, 0))

    def _loop() -> None:
        while True:
            stats = _DBPoolCollector._read_pool()
            if stats is not None:
                for key, gauge in gauges.items():
                    gauge.set(stats[key])
            _tick_gc()
            if stop.wait(timeout=_POOL_GAUGE_INTERVAL_S):
                return

    threading.Thread(target=_loop, name="db-pool-gauges", daemon=True).start()
    return stop


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # На Windows os.kill(pid, 0) не проверяет, а завершает процесс (TerminateProcess).
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def collect_dead_process_metrics(path: str | None = None) -> list[int]:
    """Убрать live-gauge'и процессов, которых больше нет (упавший воркер uvicorn перезапущен).

    Счётчики и гистограммы умерших процессов остаются — их сумма не должна падать.
    Возвращает pid, чьи файлы убраны.
    """
    path = path or multiprocess_dir()
    if path is None or not os.path.isdir(path):
        return []
    from prometheus_client.multiprocess import mark_process_dead

    dead: set[int] = set()
    for name in os.listdir(path):
        if not (name.startswith("gauge_live") and name.endswith(".db")):
            continue
        try:
            pid = int(name[: -len(".db")].rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if pid != os.getpid() and not _pid_alive(pid):
            dead.add(pid)
    for pid in sorted(dead):
        mark_process_dead(pid, path)
    return sorted(dead)
