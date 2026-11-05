"""Общий запуск RQ-воркера: tracing, /metrics, recover-цикл, heartbeat и watchdog.

Процесс (ядро или сервис) передаёт свои настройки и хуки: подключение Redis, tracing,
регистрацию доменных метрик. Логирование, ``.env`` и пул БД настраивает вызывающий до вызова.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import uuid
from collections.abc import Callable
from typing import Any


def worker_name(role: str) -> str:
    """Уникальное имя RQ-воркера: в Docker у всех реплик PID=1, поэтому pid недостаточно."""
    host = (socket.gethostname() or "").strip() or role
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def run_rq_worker(
    *,
    role: str,
    queue_name: str,
    metrics_port: int,
    recover_fn: Callable[[], int],
    job_backend: str,
    job_backend_env: str,
    redis_url: str | None,
    redis_connection: Callable[[], Any],
    recover_interval_seconds: int,
    stall_exit_seconds: float,
    setup_tracing: Callable[[], object],
    shutdown_tracing: Callable[[], None],
    setup: Callable[[], None] | None = None,
    logger_name: str | None = None,
) -> None:
    """Блокирующий цикл SimpleWorker на ``queue_name`` до остановки."""
    log = logging.getLogger(logger_name or f"jobdesk.workers.{role}")
    os.environ.setdefault("OTEL_SERVICE_NAME", f"jobdesk-{role}")

    try:
        setup_tracing()
    except Exception as e:
        log.warning("OTel для %s не включён: %s", role, e)

    # Без fork (SimpleWorker): метрики в том же процессе, что и порт метрик; меньше пик RAM
    # (Worker fork'ает work horse на каждую джобу — COW-копия + счётчики теряются).
    try:
        from prometheus_client import start_http_server

        start_http_server(metrics_port)
        log.info("%s metrics on :%s/metrics", role, metrics_port)
    except Exception as e:
        log.warning("Не удалось стартовать metrics HTTP: %s", e)

    from jobdesk_platform.workers.liveness import RQ_WORKER_TTL, LivenessWorker, start_watchdog

    if setup is not None:
        setup()

    if job_backend != "rq":
        log.warning("%s=%s, но %s рассчитан на rq — продолжаем с Redis", job_backend_env, job_backend, role)
    if not (redis_url or "").strip():
        log.error("REDIS_URL обязателен для %s", role)
        sys.exit(1)

    try:
        n = recover_fn()
        log.info("Recover on start: %s job(s)", n)
    except Exception as e:
        log.warning("Recover on start failed: %s", e)

    from jobdesk_platform.workers.recover_loop import start_periodic_recover

    recover_stop = start_periodic_recover(
        recover_fn,
        interval_seconds=recover_interval_seconds,
        name=f"{role}-recover-loop",
    )
    watchdog_stop = start_watchdog(stall_exit_seconds, name=role)

    conn = redis_connection()
    name = worker_name(role)
    log.info("%s listening queue=%s (SimpleWorker, name=%s)", role, queue_name, name)
    worker = LivenessWorker([queue_name], connection=conn, name=name, worker_ttl=RQ_WORKER_TTL)
    try:
        worker.work(with_scheduler=False)
    finally:
        recover_stop.set()
        if watchdog_stop is not None:
            watchdog_stop.set()
        shutdown_tracing()
