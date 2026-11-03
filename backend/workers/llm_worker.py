"""Entrypoint RQ-воркера: ``jobdesk-llm-worker``."""

from __future__ import annotations

from backend.settings import get_settings
from backend.workers.runner import run_rq_worker


def _setup() -> None:
    from backend.core_ports import register_core_ports
    from backend.observability import init_llm_job_series, init_llm_model_series
    from backend.services.llm_job_service import ALL_KINDS

    # Ручной импорт идёт через AIEnrichmentService — без кэша LLM он не создаётся.
    # Реплики llm-worker делят один ai.requests_per_minute, а не умножают его.
    register_core_ports()
    init_llm_model_series()
    init_llm_job_series(ALL_KINDS)


def main() -> None:
    from backend.queue.rq_conn import LLM_QUEUE_NAME
    from backend.workers.recover import recover_stuck_llm_jobs

    settings = get_settings()
    run_rq_worker(
        role="llm-worker",
        queue_name=LLM_QUEUE_NAME,
        metrics_port=settings.llm_worker_metrics_port,
        recover_fn=recover_stuck_llm_jobs,
        job_backend=settings.llm_job_backend_normalized,
        job_backend_env="LLM_JOB_BACKEND",
        setup=_setup,
    )


if __name__ == "__main__":
    main()
