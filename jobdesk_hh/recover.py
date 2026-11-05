"""Поднять зависшие / потерянные из Redis ``hh.hh_sync_jobs`` обратно в очередь."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jobdesk_hh.db import jobs
from jobdesk_hh.db.session import session_scope
from jobdesk_hh.events import publish_hh_sync_event
from jobdesk_hh.settings import get_hh_settings
from jobdesk_platform.workers.recover_loop import rq_job_still_live

_LOG = logging.getLogger(__name__)


def recover_stuck_hh_sync_jobs(*, requeue_all_queued: bool = False) -> int:
    """``running`` с истёкшим lease → ``queued`` (``failed`` по лимиту попыток, ``cancelled`` при запрошенной
    отмене); старые ``queued`` → в очередь.

    ``requeue_all_queued`` — поднять и свежие ``queued`` (старт сервиса в режиме inprocess:
    потоков прошлого процесса уже нет). Возвращает число джоб, отправленных в очередь.
    """
    from jobdesk_hh.job_service import enqueue_hh_sync_job

    settings = get_hh_settings()
    rq_mode = settings.hh_sync_job_backend_normalized == "rq"
    now = datetime.now(UTC)
    stuck_queued_after = now - timedelta(minutes=max(1, settings.hh_sync_job_stuck_minutes))
    to_requeue: list[UUID] = []
    failed: list[UUID] = []
    cancelled: list[UUID] = []

    with session_scope() as session:
        for row in jobs.list_hh_sync_jobs_with_expired_lease(session, now=now):
            job_id = row.id  # reclaim делает expire_all — читаем до него
            outcome = jobs.reclaim_expired_hh_sync_job(session, job_id, now=now)
            if outcome == "queued":
                _LOG.warning("HH sync %s: recover running→queued (lease expired)", job_id)
                to_requeue.append(job_id)
            elif outcome == "failed":
                _LOG.error("HH sync %s: recover running→failed (исчерпан лимит попыток)", job_id)
                failed.append(job_id)
            elif outcome == "cancelled":
                _LOG.warning("HH sync %s: recover running→cancelled (отмена запрошена)", job_id)
                cancelled.append(job_id)
        older_than = None if requeue_all_queued else stuck_queued_after
        for row in jobs.list_queued_hh_sync_jobs(session, older_than=older_than):
            if row.id not in to_requeue:
                to_requeue.append(row.id)

    # После commit: подписчик, получив событие, перечитывает уже финальный статус.
    for job_id in failed:
        publish_hh_sync_event(job_id, status="failed")
    for job_id in cancelled:
        publish_hh_sync_event(job_id, status="cancelled")

    redis_conn = None
    if rq_mode and to_requeue:
        from jobdesk_hh.queue import get_redis_connection

        redis_conn = get_redis_connection()

    requeued = 0
    for job_id in to_requeue:
        try:
            with session_scope() as session:
                row = jobs.get_hh_sync_job(session, job_id)
                if row is None or row.status != "queued":
                    continue
                rq_job_id = row.rq_job_id
            if rq_mode and rq_job_id and rq_job_still_live(rq_job_id, redis_conn):
                continue
            enqueue_hh_sync_job(job_id)
            requeued += 1
        except Exception as e:
            _LOG.warning("HH sync recover enqueue %s failed: %s", job_id, e)

    if requeued:
        _LOG.info("HH sync recover: requeued=%s", requeued)
    return requeued
