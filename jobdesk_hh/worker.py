"""Entrypoint ``jobdesk-hh-worker``: RQ-воркер очереди ``hh`` (фоновый HH sync)."""

from __future__ import annotations


def main() -> None:
    from jobdesk.utils.env_loader import try_load_dotenv
    from jobdesk.utils.logger import setup_logging

    setup_logging()
    try_load_dotenv()

    import jobdesk_hh.observability  # noqa: F401 — счётчики hh_* в /metrics воркера
    from jobdesk_hh.queue import HH_QUEUE_NAME, get_redis_connection
    from jobdesk_hh.recover import recover_stuck_hh_sync_jobs
    from jobdesk_hh.settings import get_hh_settings
    from jobdesk_platform.tracing import setup_tracing, shutdown_tracing
    from jobdesk_platform.workers.runner import run_rq_worker

    settings = get_hh_settings()

    def _tracing() -> None:
        from fastapi import FastAPI

        setup_tracing(
            FastAPI(),
            enabled=settings.otel_enabled,
            sample_ratio=settings.otel_traces_sample_ratio,
            exclude_paths=frozenset(),
            default_service_name="jobdesk-hh-worker",
        )

    run_rq_worker(
        role="hh-worker",
        queue_name=HH_QUEUE_NAME,
        metrics_port=settings.hh_worker_metrics_port,
        recover_fn=recover_stuck_hh_sync_jobs,
        job_backend=settings.hh_sync_job_backend_normalized,
        job_backend_env="HH_SYNC_JOB_BACKEND",
        redis_url=settings.redis_url,
        redis_connection=get_redis_connection,
        recover_interval_seconds=settings.job_recover_interval_seconds,
        stall_exit_seconds=settings.worker_stall_exit_seconds,
        setup_tracing=_tracing,
        shutdown_tracing=shutdown_tracing,
        logger_name="jobdesk_hh.worker",
    )


if __name__ == "__main__":
    main()
