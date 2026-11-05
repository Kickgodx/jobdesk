"""Запуск RQ-воркера ядра: настройки ``backend`` поверх ``jobdesk_platform.workers.runner``."""

from __future__ import annotations

import socket  # noqa: F401 — тесты подменяют socket.gethostname через этот модуль
from collections.abc import Callable

from backend.settings import get_settings
from backend.tracing import setup_tracing, shutdown_tracing
from jobdesk.utils.env_loader import try_load_dotenv
from jobdesk.utils.logger import setup_logging
from jobdesk_platform.workers import runner as _impl
from jobdesk_platform.workers.runner import worker_name  # noqa: F401 — реэкспорт


def run_rq_worker(
    *,
    role: str,
    queue_name: str,
    metrics_port: int,
    recover_fn: Callable[[], int],
    job_backend: str,
    job_backend_env: str,
    setup: Callable[[], None] | None = None,
) -> None:
    """Блокирующий цикл SimpleWorker на ``queue_name`` до остановки."""
    from backend.db.session import set_pool_role

    set_pool_role(role)
    setup_logging()
    try_load_dotenv()
    settings = get_settings()

    def _tracing() -> None:
        # Минимальный FastAPI app не нужен — поднимаем OTEL через Settings как у backend.
        from fastapi import FastAPI

        setup_tracing(FastAPI(), settings)

    def _setup() -> None:
        # Регистрация доменных метрик (counters/histograms). db_* (COUNT по таблицам) отдаёт
        # только backend; db_pool_* — и здесь (свой QueuePool).
        import backend.observability  # noqa: F401

        if setup is not None:
            setup()

    def _redis():
        from backend.queue.rq_conn import get_redis_connection

        return get_redis_connection()

    _impl.run_rq_worker(
        role=role,
        queue_name=queue_name,
        metrics_port=metrics_port,
        recover_fn=recover_fn,
        job_backend=job_backend,
        job_backend_env=job_backend_env,
        redis_url=settings.redis_url,
        redis_connection=_redis,
        recover_interval_seconds=settings.job_recover_interval_seconds,
        stall_exit_seconds=settings.worker_stall_exit_seconds,
        setup_tracing=_tracing,
        shutdown_tracing=lambda: shutdown_tracing(),
        setup=_setup,
        logger_name=f"backend.workers.{role}",
    )
