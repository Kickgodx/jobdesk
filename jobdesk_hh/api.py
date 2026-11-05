"""HTTP API HH-сервиса. Вызывает только ядро (``HH_SERVICE_TOKEN``), проксируя ``/api/v1/hh/*``.

Права администратора, ``Idempotency-Key`` и SSE-тикет проверяет ядро до прокси; сюда
приходит id пользователя в ``X-JobDesk-User-Id``. Без токена открыт только ``/health``.
Ошибки — в едином формате ``detail = {code, message, retryable}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from jobdesk.config.loader import load_config
from jobdesk.contracts.hh_service import (
    USER_ID_HEADER,
    HhStatusResponse,
    HhSyncAccepted,
    HhSyncJobOut,
    HhSyncProgress,
    HhSyncRequest,
    HhSyncResponse,
)
from jobdesk_hh import job_service
from jobdesk_hh.db import jobs, listings
from jobdesk_hh.db.models import HhSyncJobRow
from jobdesk_hh.db.session import session_scope
from jobdesk_hh.events import channel_for
from jobdesk_hh.query import HhSearchQueryError, resolve_hh_search_query
from jobdesk_hh.settings import get_hh_settings
from jobdesk_hh.sync import HhSyncNotConfigured, ensure_hh_ready
from jobdesk_hh.window import HhSyncWindowError, resolve_hh_search_window
from jobdesk_platform.errors import (
    CODE_NOT_FOUND,
    CODE_UPSTREAM_UNAVAILABLE,
    CODE_VALIDATION,
    http_error,
    register_exception_handlers,
)
from jobdesk_platform.pubsub_hub import PubSubUnavailable, get_pubsub_hub
from jobdesk_platform.service_auth import require_service_token
from jobdesk_platform.sse import (
    DB_POLL_INTERVAL_S,
    HEARTBEAT_INTERVAL_S,
    IDLE_DB_CHECK_S,
    PUBSUB_WAIT_S,
    SSE_DONE,
    SSE_HEADERS,
    SSE_PING,
    Ticker,
    sse_event,
)

_LOG = logging.getLogger(__name__)

_NOT_FOUND_MESSAGE = "HH sync job not found"


def _not_found():
    return http_error(404, CODE_NOT_FOUND, _NOT_FOUND_MESSAGE)


def job_to_out(row: HhSyncJobRow) -> HhSyncJobOut:
    params = row.params or {}
    return HhSyncJobOut(
        id=row.id,
        status=row.status,
        window=params.get("window") or "all",
        period=params.get("period"),
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        query=params.get("query") or "main",
        search_text=params.get("text"),
        progress=HhSyncProgress.model_validate(row.progress) if row.progress else None,
        result=HhSyncResponse.model_validate(row.result) if row.result else None,
        error_message=row.error_message,
        attempts=int(row.attempts or 0),
        cancel_requested=bool(row.cancel_requested),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


# --- жизненный цикл ---------------------------------------------------------------------


def _start_metrics_server(port: int):
    from prometheus_client import start_http_server

    from jobdesk_hh.observability import register_db_stats_collector

    register_db_stats_collector()
    server, _thread = start_http_server(port)
    _LOG.info("HH-сервис: метрики на :%s/metrics", port)
    return server


def _start_inprocess_recovery() -> threading.Event:
    """Поднять зависшие sync при старте и запустить периодический recover (режим ``inprocess``)."""
    from jobdesk_hh.recover import recover_stuck_hh_sync_jobs
    from jobdesk_platform.workers.recover_loop import start_periodic_recover

    try:
        # Потоки прошлого процесса не пережили рестарт — queued поднимаем сразу.
        n = recover_stuck_hh_sync_jobs(requeue_all_queued=True)
        if n:
            _LOG.info("hh-recover-loop: recover on start, requeued=%s", n)
    except Exception as e:
        _LOG.warning("hh-recover-loop: recover on start failed: %s", e)
    return start_periodic_recover(
        recover_stuck_hh_sync_jobs,
        interval_seconds=get_hh_settings().job_recover_interval_seconds,
        name="hh-recover-loop",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from jobdesk_hh.maintenance import start_maintenance_loop
    from jobdesk_hh.migrate import upgrade_head

    settings = get_hh_settings()
    # Ошибка миграции останавливает старт сервиса.
    await run_in_threadpool(upgrade_head, settings.database_url)
    stops: list[threading.Event] = []
    metrics_server = None
    if settings.hh_service_metrics_port:
        try:
            metrics_server = _start_metrics_server(settings.hh_service_metrics_port)
        except Exception as e:
            _LOG.warning("HH-сервис: сервер метрик не запущен: %s", e)
    if settings.hh_sync_job_backend_normalized == "inprocess":
        stops.append(_start_inprocess_recovery())
    if settings.hh_maintenance_interval_seconds > 0:
        stops.append(start_maintenance_loop(interval_seconds=settings.hh_maintenance_interval_seconds))
    _LOG.info("HH-сервис готов (sync: %s).", settings.hh_sync_job_backend_normalized)
    try:
        yield
    finally:
        for stop in stops:
            stop.set()
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()


app = FastAPI(
    title="JobDesk HH service",
    version="0.1.0",
    lifespan=lifespan,
    # Свой tracing (jobdesk_platform.tracing), без встроенного в FastAPI — иначе дубли span'ов.
    telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
)
register_exception_handlers(app)


@app.get("/health")
def health() -> dict:
    """Здоровье без токена: 200 и ``degraded``, если база не отвечает."""
    database = True
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception as e:
        _LOG.warning("HH-сервис: база не отвечает: %s", e)
        database = False
    return {"status": "ok" if database else "degraded", "database": database}


router = APIRouter(
    prefix="/v1",
    dependencies=[Depends(require_service_token(lambda: get_hh_settings().hh_service_token))],
)


@router.get("/status", response_model=HhStatusResponse)
def hh_status() -> HhStatusResponse:
    settings = get_hh_settings()
    cfg = load_config(settings.config_path)
    with session_scope() as session:
        counts = listings.listing_counts(session)
    return HhStatusResponse(
        enabled=cfg.hh.enabled,
        token_configured=bool((settings.hh_api_token or "").strip()),
        email_configured=bool((settings.hh_contact_email or "").strip()),
        listing_ttl_days=cfg.hh.listing_ttl_days,
        search_text=cfg.hh.search.text,
        search_text_no_python=cfg.hh.search.text_no_python,
        professional_role=list(cfg.hh.search.professional_role),
        listings_total=counts.total,
        listings_with_detail=counts.with_detail,
        listings_without_detail=counts.without_detail,
    )


@router.post("/sync", response_model=HhSyncAccepted, status_code=202)
def hh_sync(
    body: HhSyncRequest = HhSyncRequest(),
    user_id: int | None = Header(default=None, alias=USER_ID_HEADER),
) -> HhSyncAccepted:
    """Поставить HH sync в очередь: 202 ``{job_id}``, 409 — sync уже активен."""
    try:
        resolved = resolve_hh_search_window(window=body.window, date_from=body.date_from, date_to=body.date_to)
    except HhSyncWindowError as exc:
        raise http_error(422, CODE_VALIDATION, str(exc)) from exc

    settings = get_hh_settings()
    cfg = load_config(settings.config_path)
    try:
        query = resolve_hh_search_query(cfg.hh.search, query=body.query, text=body.text)
    except HhSearchQueryError as exc:
        raise http_error(422, CODE_VALIDATION, str(exc)) from exc
    try:
        ensure_hh_ready(settings, cfg.hh)
    except HhSyncNotConfigured as exc:
        raise http_error(503, CODE_UPSTREAM_UNAVAILABLE, str(exc), retryable=True) from exc

    job_id = job_service.start_hh_sync_job(resolved, query, requested_by=user_id)
    return HhSyncAccepted(job_id=job_id, status="queued")


@router.get("/sync-jobs", response_model=list[HhSyncJobOut])
def list_hh_sync_jobs(limit: int = Query(default=10, ge=1, le=100)) -> list[HhSyncJobOut]:
    """Последние sync, новые первыми."""
    with session_scope() as session:
        return [job_to_out(row) for row in jobs.list_hh_sync_jobs(session, limit=limit)]


@router.get("/sync-jobs/{job_id}", response_model=HhSyncJobOut)
def get_hh_sync_job(job_id: uuid.UUID) -> HhSyncJobOut:
    out = _load_job_out(job_id)
    if out is None:
        raise _not_found()
    return out


@router.post("/sync-jobs/{job_id}/cancel", response_model=HhSyncJobOut)
def cancel_hh_sync_job(job_id: uuid.UUID) -> HhSyncJobOut:
    """Отменить sync; ответ — его текущее состояние (и для уже завершённого)."""
    if not job_service.cancel_hh_sync_job(job_id):
        raise _not_found()
    out = _load_job_out(job_id)
    if out is None:
        raise _not_found()
    return out


# --- SSE ----------------------------------------------------------------------------------

_NOT_FOUND_EVENT = sse_event("error", json.dumps({"detail": _NOT_FOUND_MESSAGE}))


def _load_job_out(job_id: uuid.UUID) -> HhSyncJobOut | None:
    """Синхронное чтение — из стрима только через ``run_in_threadpool``, не в event loop."""
    with session_scope() as session:
        row = jobs.get_hh_sync_job(session, job_id)
        return job_to_out(row) if row is not None else None


def _status_line(out: HhSyncJobOut) -> str:
    return sse_event("status", out.model_dump_json())


def _terminal(out: HhSyncJobOut) -> bool:
    return out.status in jobs.TERMINAL_STATUSES


def _redis_factory():
    from jobdesk_hh.queue import get_async_redis_client

    return get_async_redis_client()


async def _sse_hh_sync(job_id: uuid.UUID, initial: HhSyncJobOut) -> AsyncIterator[str]:
    yield _status_line(initial)
    if _terminal(initial):
        yield SSE_DONE
        return
    if (get_hh_settings().redis_url or "").strip():
        try:
            async for chunk in _sse_hh_sync_redis(job_id):
                yield chunk
            return
        except PubSubUnavailable as e:
            _LOG.warning("SSE HH sync %s: Redis pub/sub недоступен (%s) — опрос БД", job_id, e)
    async for chunk in _sse_hh_sync_db_poll(job_id, last=initial):
        yield chunk


async def _sse_hh_sync_db_poll(job_id: uuid.UUID, *, last: HhSyncJobOut) -> AsyncIterator[str]:
    """Опрос БД раз в секунду: статус при изменении, до терминального."""
    heartbeat = Ticker(HEARTBEAT_INTERVAL_S)
    while True:
        await asyncio.sleep(DB_POLL_INTERVAL_S)
        out = await run_in_threadpool(_load_job_out, job_id)
        if out is None:
            yield _NOT_FOUND_EVENT
            return
        if out != last:
            last = out
            heartbeat.reset()
            yield _status_line(out)
        if _terminal(out):
            yield SSE_DONE
            return
        if heartbeat.due():
            yield SSE_PING


async def _sse_hh_sync_redis(job_id: uuid.UUID) -> AsyncIterator[str]:
    heartbeat = Ticker(HEARTBEAT_INTERVAL_S)
    db_check = Ticker(IDLE_DB_CHECK_S)
    async with get_pubsub_hub(_redis_factory, name="hh").subscribe(channel_for(job_id)) as sub:
        while True:
            payload = await sub.get(timeout=PUBSUB_WAIT_S)
            if payload is None and not db_check.due():
                if heartbeat.due():
                    yield SSE_PING
                continue
            # Событие (или редкая сверка в тишине): полный статус — из БД.
            out = await run_in_threadpool(_load_job_out, job_id)
            if out is None:
                yield _NOT_FOUND_EVENT
                return
            if payload is not None or _terminal(out):
                heartbeat.reset()
                yield _status_line(out)
            if _terminal(out):
                yield SSE_DONE
                return


@router.get("/sync-jobs/{job_id}/events")
async def hh_sync_job_events(job_id: uuid.UUID) -> StreamingResponse:
    """SSE: события ``status`` (джоба целиком: статус, прогресс, итог) до ``done``."""
    initial = await run_in_threadpool(_load_job_out, job_id)
    if initial is None:
        raise _not_found()
    return StreamingResponse(_sse_hh_sync(job_id, initial), media_type="text/event-stream", headers=SSE_HEADERS)


app.include_router(router)
