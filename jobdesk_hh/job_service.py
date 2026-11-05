"""Запуск фонового HH sync: строка ``hh.hh_sync_jobs`` + enqueue (RQ ``hh``) или поток в процессе сервиса."""

from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy.exc import IntegrityError

from jobdesk_hh.db import jobs
from jobdesk_hh.db.session import session_scope
from jobdesk_hh.events import publish_hh_sync_event
from jobdesk_hh.query import ResolvedHhSearchQuery
from jobdesk_hh.settings import get_hh_settings
from jobdesk_hh.window import ResolvedHhSearchWindow
from jobdesk_platform.errors import CODE_CONFLICT_ACTIVE_RUN, AppError
from jobdesk_platform.tracing import current_traceparent

_LOG = logging.getLogger(__name__)

_ACTIVE_MSG = "HH sync уже выполняется. Дождитесь его завершения."


def _conflict() -> AppError:
    return AppError(CODE_CONFLICT_ACTIVE_RUN, _ACTIVE_MSG, status_code=409, retryable=False)


def enqueue_hh_sync_job(job_id: uuid.UUID, traceparent: str | None = None) -> None:
    """Отправить ``queued``-джобу исполнителю. Бросает исключение, если очередь недоступна."""
    from jobdesk_hh.tasks import run_hh_sync_job

    settings = get_hh_settings()
    if settings.hh_sync_job_backend_normalized == "rq":
        from jobdesk_hh.queue import get_hh_queue

        rq_job = get_hh_queue().enqueue(
            run_hh_sync_job,
            str(job_id),
            traceparent,
            job_timeout=settings.hh_sync_rq_job_timeout_seconds,
            result_ttl=3600,
            failure_ttl=86400,
        )
        with session_scope() as session:
            jobs.set_hh_sync_job_rq_job_id(session, job_id, str(rq_job.id))
        _LOG.info("HH sync %s поставлен в RQ hh (job=%s)", job_id, rq_job.id)
    else:
        threading.Thread(
            target=run_hh_sync_job,
            args=(str(job_id), traceparent),
            name=f"hh-sync-{job_id}",
            daemon=True,
        ).start()
        _LOG.info("HH sync %s запущен inprocess", job_id)


def cancel_hh_sync_job(job_id: uuid.UUID) -> bool:
    """Отмена sync: ``queued`` — сразу, ``running`` — флагом для воркера. False — sync нет.

    Завершённый sync не меняется (идемпотентно).
    """
    with session_scope() as session:
        row = jobs.get_hh_sync_job(session, job_id)
        if row is None:
            return False
        was_queued = row.status == "queued"
        row = jobs.request_hh_sync_job_cancel(session, job_id)
        status, rq_job_id = row.status, row.rq_job_id
    if was_queued and status == "cancelled":
        if rq_job_id:
            try:
                from rq.job import Job

                from jobdesk_hh.queue import get_redis_connection

                Job.fetch(rq_job_id, connection=get_redis_connection()).cancel()
            except Exception as e:
                # Не критично: воркер не возьмёт sync — claim требует queued.
                _LOG.debug("HH sync %s: RQ job не снята: %s", job_id, e)
        publish_hh_sync_event(job_id, status="cancelled")
    _LOG.info("HH sync %s: запрошена отмена (status=%s)", job_id, status)
    return True


def start_hh_sync_job(
    window: ResolvedHhSearchWindow,
    query: ResolvedHhSearchQuery | None = None,
    *,
    requested_by: int | None,
) -> uuid.UUID:
    """Создать джобу и поставить в очередь. 409, если sync уже активен.

    Без ``query`` воркер возьмёт основной запрос ``hh.search.text`` на момент запуска.
    """
    params = {
        "window": window.window,
        "period": window.period,
        "date_from": window.date_from,
        "date_to": window.date_to,
    }
    if query is not None:
        params["query"] = query.query
        params["text"] = query.text
    with session_scope() as session:
        if jobs.has_active_hh_sync_job(session):
            raise _conflict()
        try:
            job_id = jobs.create_hh_sync_job(session, params=params, requested_by=requested_by).id
        except IntegrityError as e:
            raise _conflict() from e

    try:
        enqueue_hh_sync_job(job_id, current_traceparent())
    except Exception:
        with session_scope() as session:
            jobs.fail_hh_sync_job(session, job_id, "Failed to enqueue job")
        publish_hh_sync_event(job_id, status="failed")
        raise
    return job_id
