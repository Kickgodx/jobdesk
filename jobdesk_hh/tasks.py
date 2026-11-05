"""Исполнение фонового HH sync (импортируется RQ по dotted-path ``jobdesk_hh.tasks.run_hh_sync_job``)."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from jobdesk.config.loader import load_config
from jobdesk.hh.client import HHApiError
from jobdesk_hh import core_client as core_client_module
from jobdesk_hh.db import jobs
from jobdesk_hh.db.session import session_scope
from jobdesk_hh.events import publish_hh_sync_event
from jobdesk_hh.query import ResolvedHhSearchQuery
from jobdesk_hh.settings import get_hh_settings
from jobdesk_hh.sync import HhSyncConflict, HhSyncResult, run_hh_sync
from jobdesk_hh.window import ResolvedHhSearchWindow
from jobdesk_platform.workers import liveness

_LOG = logging.getLogger(__name__)
_TRACER = trace.get_tracer(__name__)

_LEASE_HEARTBEAT_S = 30.0
_PROGRESS_MIN_INTERVAL_S = 1.0
# Флаг отмены читается из БД не чаще раза в секунду (проверка — на каждой странице / группе).
_CANCEL_CHECK_INTERVAL_S = 1.0
_ERROR_MESSAGE_MAX = 500


class HhSyncLeaseLost(RuntimeError):
    """Lease перехвачен recover: sync продолжает другой воркер — этот прекращает работу."""


def _result_dict(result: HhSyncResult | None) -> dict | None:
    return dataclasses.asdict(result) if result is not None else None


def _progress_dict(phase: str, result: HhSyncResult, done: int, total: int) -> dict:
    return {
        "phase": phase,
        "done": done,
        "total": total,
        "searched": result.searched,
        "pages": result.pages,
        "unique": result.unique,
        "duplicates": result.duplicates,
        "details_fetched": result.details_fetched,
        "details_skipped": result.details_skipped,
        "vacancies_inserted": result.vacancies_inserted,
        "vacancies_updated": result.vacancies_updated,
        "errors": len(result.errors),
    }


def _error_message(exc: Exception) -> str:
    if isinstance(exc, HHApiError):
        return f"HH API error: HTTP {exc.status_code}"
    if isinstance(exc, HhSyncConflict):
        return str(exc)
    msg = f"{type(exc).__name__}: {exc}"
    return msg if len(msg) <= _ERROR_MESSAGE_MAX else msg[: _ERROR_MESSAGE_MAX - 1] + "…"


def run_hh_sync_job(job_id: str, traceparent: str | None = None) -> None:
    """Claim queued ``hh.hh_sync_jobs`` и выполнить sync (поток сервиса или RQ hh-worker)."""
    jid = uuid.UUID(str(job_id))
    ctx = None
    if traceparent:
        try:
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

            ctx = TraceContextTextMapPropagator().extract(carrier={"traceparent": traceparent})
        except Exception:
            ctx = None
    with _TRACER.start_as_current_span(
        "hh_sync_job",
        context=ctx,
        kind=SpanKind.CONSUMER,
        attributes={"hh.job_id": str(jid)},
    ) as span:
        _run_traced(jid, span)


def _run_traced(job_id: uuid.UUID, span) -> None:
    lease_owner = jobs.new_lease_owner()
    with session_scope() as session:
        claimed = jobs.claim_hh_sync_job(session, job_id, lease_owner=lease_owner)
        if claimed is None:
            return
        params = dict(claimed.params or {})
        attempt = claimed.attempts
    publish_hh_sync_event(job_id, status="running")
    _LOG.info(
        "HH sync %s: старт (попытка %s, окно %s, запрос %s)",
        job_id,
        attempt,
        params.get("window"),
        params.get("query") or "main",
    )

    stop_lease = threading.Event()
    lease_lost = threading.Event()

    def _lease_loop() -> None:
        while not stop_lease.wait(timeout=_LEASE_HEARTBEAT_S):
            try:
                with session_scope() as session:
                    ok = jobs.touch_hh_sync_job_lease(session, job_id, lease_owner=lease_owner)
            except Exception as e:
                _LOG.debug("HH sync %s lease heartbeat: %s", job_id, e)
                continue  # транзиентная ошибка — не сдаём lease
            if not ok:
                if not stop_lease.is_set():
                    _LOG.warning("HH sync %s: lease lost (fencing) — прерываем", job_id)
                    lease_lost.set()
                return
            liveness.beat()

    lease_thread = threading.Thread(target=_lease_loop, name=f"hh-sync-lease-{job_id}", daemon=True)
    lease_thread.start()

    latest: HhSyncResult | None = None
    last_write = 0.0

    def on_progress(phase: str, result: HhSyncResult, done: int, total: int) -> None:
        nonlocal latest, last_write
        latest = result
        if lease_lost.is_set():
            raise HhSyncLeaseLost("lease lost")
        now = time.monotonic()
        final = phase == "purge"
        if not final and now - last_write < _PROGRESS_MIN_INTERVAL_S:
            return
        last_write = now
        try:
            with session_scope() as session:
                ok = jobs.patch_hh_sync_job_progress(
                    session, job_id, _progress_dict(phase, result, done, total), lease_owner=lease_owner
                )
        except Exception as e:
            # Прогресс необязателен: транзиентный сбой БД не должен ронять sync.
            _LOG.warning("HH sync %s: прогресс не записан: %s", job_id, e)
            return
        if not ok:
            lease_lost.set()
            raise HhSyncLeaseLost("lease lost")
        publish_hh_sync_event(job_id, status="running")

    cancel_seen = False
    last_cancel_check = 0.0

    def should_stop() -> bool:
        nonlocal cancel_seen, last_cancel_check
        if cancel_seen:
            return True
        now = time.monotonic()
        if now - last_cancel_check < _CANCEL_CHECK_INTERVAL_S:
            return False
        last_cancel_check = now
        try:
            with session_scope() as session:
                cancel_seen = jobs.is_hh_sync_job_cancel_requested(session, job_id)
        except Exception as e:
            # Транзиентный сбой БД не роняет sync: проверим на следующем шаге.
            _LOG.debug("HH sync %s: флаг отмены не прочитан: %s", job_id, e)
        return cancel_seen

    status = "failed"
    error: str | None = None
    try:
        window = ResolvedHhSearchWindow(
            window=params.get("window") or "all",
            period=params.get("period"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
        )
        settings = get_hh_settings()
        config = load_config(settings.config_path)
        # Текст сохранён при постановке; у джоб без него — основной запрос из конфига.
        query = ResolvedHhSearchQuery(
            query=params.get("query") or "main",
            text=params.get("text") or config.hh.search.text,
        )
        core = core_client_module.core_client()
        try:
            with session_scope() as session:
                latest = run_hh_sync(
                    session,
                    config=config,
                    settings=settings,
                    core=core,
                    search_window=window,
                    search_query=query,
                    progress_callback=on_progress,
                    should_stop=should_stop,
                )
        finally:
            core.close()
        status = "cancelled" if cancel_seen else "completed"
        if cancel_seen:
            _LOG.info("HH sync %s: отменён, итог до остановки сохранён", job_id)
    except HhSyncLeaseLost:
        _LOG.warning("HH sync %s: остановлен — lease перехвачен recover", job_id)
    except Exception as e:
        error = _error_message(e)
        _LOG.exception("HH sync %s упал: %s", job_id, e)
    finally:
        stop_lease.set()
        lease_thread.join(timeout=1.0)

    span.set_attribute("hh.status", status)
    if lease_lost.is_set():
        span.set_status(Status(StatusCode.ERROR, "lease lost"))
        return
    with session_scope() as session:
        wrote = jobs.finish_hh_sync_job(
            session,
            job_id,
            status,
            lease_owner=lease_owner,
            result=_result_dict(latest),
            error_message=error,
        )
    if not wrote:
        _LOG.warning("HH sync %s: finalize no-op (fencing)", job_id)
        return
    if status == "failed":
        span.set_status(Status(StatusCode.ERROR, error or "failed"))
    publish_hh_sync_event(job_id, status=status)
