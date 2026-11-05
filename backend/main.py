"""FastAPI backend: парсинг, AI, PostgreSQL."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import trace
from prometheus_fastapi_instrumentator import Instrumentator

import jobdesk.observability.llm_metrics  # noqa: F401 — регистрация llm_* в /metrics
from backend.api.routes import (
    ai_cache,
    analytics,
    auth,
    channels,
    companies,
    config,
    digests,
    filters,
    fx,
    health,
    hh,
    internal,
    llm_jobs,
    market_chat,
    market_map,
    resumes,
    runs,
    vacancies,
)
from backend.db import fx_repository as fx_repo
from backend.db import repository as repo
from backend.db import vacancy_repository as vacancy_repo
from backend.db.company_profile import backfill_company_keys
from backend.db.session import init_db, session_scope
from backend.errors import register_exception_handlers
from backend.http_logging import HttpLoggingMiddleware
from backend.maintenance import MaintenanceDuties
from backend.maintenance import runs_here_inprocess as maintenance_runs_inprocess
from backend.observability import register_db_stats_collector
from backend.overload import OverloadMiddleware, RequestSlots
from backend.settings import check_api_workers, check_exposure, get_settings
from backend.tracing import setup_tracing, shutdown_tracing
from jobdesk.config.loader import load_config
from jobdesk.utils.env_loader import try_load_dotenv
from jobdesk.utils.logger import setup_logging

setup_logging()
try_load_dotenv()
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
_TRACER = trace.get_tracer(__name__)


def _sync_fx_on_startup() -> None:
    """Фоновая подгрузка курсов ЦБ — не блокирует приём HTTP-запросов."""
    log = logging.getLogger(__name__)
    try:
        cfg = load_config(get_settings().config_path)
        if not cfg.fx.enabled or cfg.fx.sync_days_on_startup <= 0:
            return
        with _TRACER.start_as_current_span("fx.sync_on_startup") as span:
            with session_scope() as session:
                n = fx_repo.sync_recent_days(session, cfg.fx)
            span.set_attribute("fx.rates_synced", n)
        log.info(
            "Курсы ЦБ: синхронизировано записей за последние %s дн.: %s",
            cfg.fx.sync_days_on_startup,
            n,
        )
    except Exception as e:
        log.warning("Курсы ЦБ при старте не загружены: %s", e)


def _cleanup_expired_vacancies() -> None:
    """Применить TTL вакансий (архивацию), бэкфилл грейдов HH и ключей компаний.

    Кэш выдачи HH и историю HH sync чистит HH-сервис (``jobdesk_hh.maintenance``).
    """
    try:
        with session_scope() as session:
            repo.archive_expired_vacancies(session)
            vacancy_repo.backfill_hh_grades_from_experience(session)
            backfill_company_keys(session)
    except Exception as e:
        logging.getLogger(__name__).warning("TTL вакансий не применён: %s", e)


def _periodic_cleanup() -> None:
    """TTL вакансий и очистка служебных таблиц (кэш LLM, LLM-джобы, idempotency, прогоны, курсы)."""
    _cleanup_expired_vacancies()
    try:
        from backend.services.db_cleanup import purge_service_tables

        cfg = load_config(get_settings().config_path)
        purge_service_tables(cfg.retention, fx_sync_days=cfg.fx.sync_days_on_startup)
    except Exception as e:
        logging.getLogger(__name__).warning("Очистка служебных таблиц не выполнена: %s", e)


_CLEANUP_INTERVAL_S = 60 * 60


class _LeaderDuties:
    """Общая для процессов API работа: её выполняет только лидер (``backend.leader``).

    Получив роль — очистка (синхронно, до «Backend готов» при старте), фоновая
    синхронизация курсов и сервер ``/metrics``; дальше раз в час — очистка, а в
    multiprocess-режиме метрик — уборка файлов умерших процессов. При
    ``MAINTENANCE_BACKEND=inprocess`` — ещё и обслуживание каталога (``backend.maintenance``).
    """

    def __init__(self) -> None:
        self._metrics_server = None
        self._last_cleanup = 0.0
        # Обслуживание каталога (backend/maintenance.py) — здесь, только если нет отдельного воркера.
        self._maintenance = MaintenanceDuties(threaded=True) if maintenance_runs_inprocess() else None

    def _tick_maintenance(self) -> None:
        if self._maintenance is not None:
            self._maintenance.tick()

    def on_acquire(self) -> None:
        import time

        _periodic_cleanup()
        self._last_cleanup = time.monotonic()
        threading.Thread(target=_sync_fx_on_startup, name="fx-startup-sync", daemon=True).start()
        self._metrics_server = _start_metrics_server()
        self._tick_maintenance()

    def on_tick(self) -> None:
        import time

        from backend.observability import collect_dead_process_metrics

        collect_dead_process_metrics()
        if time.monotonic() - self._last_cleanup >= _CLEANUP_INTERVAL_S:
            self._last_cleanup = time.monotonic()
            _periodic_cleanup()
        self._tick_maintenance()

    def on_lose(self) -> None:
        server, self._metrics_server = self._metrics_server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def _start_inprocess_job_recovery() -> list[threading.Event]:
    """Поднять зависшие джобы и запустить периодический recover для backend'ов ``inprocess``.

    В режиме ``rq`` это делают воркеры. В ``inprocess`` воркеров нет: без recover
    в API прогон, оборванный рестартом, навсегда остаётся ``running`` (вечный
    409 CONFLICT_ACTIVE_RUN), а LLM-джобы съедают лимит пользователя.
    """
    from backend.workers.recover import recover_stuck_llm_jobs
    from backend.workers.recover_loop import start_periodic_recover
    from backend.workers.recover_parse import recover_stuck_parse_runs

    log = logging.getLogger(__name__)
    settings = get_settings()
    stops: list[threading.Event] = []
    for job_backend, recover_fn, name in (
        (settings.parse_job_backend_normalized, recover_stuck_parse_runs, "parse-recover-loop"),
        (settings.llm_job_backend_normalized, recover_stuck_llm_jobs, "llm-recover-loop"),
    ):
        if job_backend != "inprocess":
            continue
        try:
            # Потоки прошлого процесса не пережили рестарт — queued поднимаем сразу.
            # running ждёт истечения lease: его подберёт периодический цикл.
            n = recover_fn(requeue_all_queued=True)
            if n:
                log.info("%s: recover on start, requeued=%s", name, n)
        except Exception as e:
            log.warning("%s: recover on start failed: %s", name, e)
        stops.append(
            start_periodic_recover(
                recover_fn,
                interval_seconds=settings.job_recover_interval_seconds,
                name=name,
            )
        )
    return stops


def _start_metrics_server():
    """``/metrics`` на ``API_METRICS_PORT`` — отдельно от порта API, как у воркеров.

    Порт API публикуется клиентам, порт метрик — нет: в compose Prometheus
    ходит на него по Docker-сети. Отдаёт тот же ``REGISTRY``, куда пишут
    Instrumentator (HTTP), доменные и ``db_*``-метрики; при нескольких процессах
    API — их сумму (``metrics_registry``). Поднимает только лидер (``_LeaderDuties``).
    """
    s = get_settings()
    if not s.api_metrics_port:
        return None
    from prometheus_client import start_http_server

    from backend.observability import metrics_registry

    log = logging.getLogger(__name__)
    try:
        server, _thread = start_http_server(s.api_metrics_port, addr=s.api_host, registry=metrics_registry())
    except OSError as e:
        log.warning("Не удалось стартовать metrics HTTP на %s:%s: %s", s.api_host, s.api_metrics_port, e)
        return None
    log.info("backend metrics on %s:%s/metrics", s.api_host, s.api_metrics_port)
    return server


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log = logging.getLogger(__name__)
    warning = check_exposure(get_settings())
    if warning:
        log.warning(warning)
    check_api_workers(get_settings())
    # Потоков не больше, чем соединений пула процесса (при одном процессе — прежние 40).
    import anyio.to_thread

    from backend.db.session import api_thread_limit

    slots = api_thread_limit(get_settings())
    anyio.to_thread.current_default_thread_limiter().total_tokens = slots
    # Запросов в работе не больше, чем потоков и соединений: без этого всплеск даёт тупик
    # «поток ↔ соединение» (backend/overload.py).
    _app.state.request_slots = RequestSlots(slots, get_settings().api_queue_timeout_seconds)
    from backend.core_ports import register_core_ports

    # LLM в inprocess-режиме идёт в этом процессе; RPM общий с воркерами.
    register_core_ports()
    from backend.observability import init_llm_job_series, init_llm_model_series
    from backend.services.llm_job_service import ALL_KINDS

    init_llm_model_series()
    init_llm_job_series(ALL_KINDS, backend=get_settings().llm_job_backend_normalized)
    env = get_settings()
    log.info(
        "APP_ENV=%s: регистрация новых пользователей %s, тела HTTP в логах %s",
        env.app_env,
        "открыта" if env.auth_registration_open else "закрыта",
        "пишутся" if env.http_log_bodies else "не пишутся",
    )
    log.info("Инициализация БД…")
    init_db()
    from backend.leader import ApiLeader
    from backend.observability import start_pool_gauges

    duties = _LeaderDuties()
    leader = ApiLeader(on_acquire=duties.on_acquire, on_lose=duties.on_lose, on_tick=duties.on_tick)
    # Первая попытка — синхронно: в одном процессе очистка при старте идёт до «Backend готов».
    if not leader.try_become_leader():
        log.info("Лидер API — другой процесс: очистка, курсы и /metrics выполняются там")
    leader.start()
    pool_gauges_stop = start_pool_gauges()
    log.info("Backend готов.")
    recover_stops = _start_inprocess_job_recovery()
    try:
        yield
    finally:
        for stop in recover_stops:
            stop.set()
        if pool_gauges_stop is not None:
            pool_gauges_stop.set()
        leader.stop()
        from backend.services.interactive_llm import reset_interactive_client
        from backend.services.llm_job_service import llm_job_runner

        llm_job_runner.reset()
        reset_interactive_client()
        shutdown_tracing()


app = FastAPI(
    title="JobDesk API",
    version="0.2.0",
    description="Backend: Telethon parsing, AI enrichment, PostgreSQL storage",
    lifespan=lifespan,
    # FastAPI >= 0.142 трассирует сам и добавляет свой OTLP-экспортёр к глобальному
    # provider'у — с нашей инструментацией (backend/tracing.py) это дубли span'ов
    # и трассы без исключений путей. Старые версии кладут параметр в app.extra.
    telemetry={
        "tracing": False,
        "metrics": False,
        "logs": False,
        "auto_configure": False,
    },
)

settings = get_settings()
register_exception_handlers(app)
# Самое внутреннее middleware: 503 перегрузки проходят через CORS, HTTP-журнал, метрики и трассы.
app.add_middleware(OverloadMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Export-Truncated"],
)
app.add_middleware(
    HttpLoggingMiddleware,
    log_bodies=settings.http_log_bodies,
    body_limit=settings.http_log_body_limit,
    exclude_paths=settings.http_log_exclude_path_set,
)
setup_tracing(app, settings)
# db_* gauges (COUNT по таблицам) — только в процессе API, см. register_db_stats_collector.
register_db_stats_collector()

app.include_router(auth.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")
app.include_router(config.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(runs.sse_router, prefix="/api/v1")
app.include_router(vacancies.router, prefix="/api/v1")
app.include_router(hh.router, prefix="/api/v1")
app.include_router(hh.sse_router, prefix="/api/v1")
app.include_router(ai_cache.router, prefix="/api/v1")
app.include_router(fx.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(digests.router, prefix="/api/v1")
app.include_router(channels.router, prefix="/api/v1")
app.include_router(companies.router, prefix="/api/v1")
app.include_router(filters.router, prefix="/api/v1")
app.include_router(resumes.resumes_router, prefix="/api/v1")
app.include_router(llm_jobs.router, prefix="/api/v1")
app.include_router(market_chat.router, prefix="/api/v1")
app.include_router(market_map.router, prefix="/api/v1")
# Внутренний API для сервисов — вне /api/v1 (наружу не проксируется), с сервисным токеном.
app.include_router(internal.router)

# Только сбор HTTP-метрик в REGISTRY; отдаёт их сервер на API_METRICS_PORT (_start_metrics_server).
Instrumentator(
    should_group_status_codes=False,
    should_ignore_untemplated=True,
    should_instrument_requests_inprogress=True,
    excluded_handlers=[
        "/api/v1/health",
        "/docs",
        "/openapi.json",
        "/redoc",
    ],
).instrument(app)


def _prepare_multiprocess_metrics_dir() -> str:
    """Пустой каталог multiprocess-метрик; путь — в окружение, его унаследуют процессы uvicorn.

    Файлы прошлого запуска удаляются: иначе счётчики продолжили бы старые значения.
    """
    import os
    import shutil
    import tempfile

    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or os.path.join(tempfile.gettempdir(), "jobdesk-api-metrics")
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = path
    return path


def main() -> None:
    import uvicorn

    s = get_settings()
    check_exposure(s)
    check_api_workers(s)
    if s.api_workers > 1:
        path = _prepare_multiprocess_metrics_dir()
        logging.getLogger(__name__).info("API: %s процессов, multiprocess-метрики в %s", s.api_workers, path)
        # Строка импорта обязательна для нескольких воркеров: каждый процесс импортирует
        # модуль заново (spawn) — и уже с PROMETHEUS_MULTIPROC_DIR в окружении.
        uvicorn.run(
            "backend.main:app",
            host=s.api_host,
            port=s.api_port,
            workers=s.api_workers,
            reload=False,
            log_config=None,
        )
        return
    # Объект, а не строка "backend.main:app": при `python -m backend.main` строка
    # заставила бы uvicorn импортировать модуль второй раз — второй app без
    # OTel-инструментации (её получил первый, из `__main__`).
    uvicorn.run(
        app,
        host=s.api_host,
        port=s.api_port,
        reload=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
