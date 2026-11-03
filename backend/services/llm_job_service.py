"""Очередь фоновых LLM-джоб: in-process ThreadPool или Redis Queue (RQ)."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from backend.db import repository as repo
from backend.db.llm_job_repository import lock_user_llm_enqueue, refund_llm_job_quota
from backend.db.session import session_scope
from backend.observability import record_llm_job, record_llm_job_enqueue
from backend.settings import get_settings
from backend.tracing import current_traceparent
from jobdesk.ai.rate_limit import interactive_daily_budget_exhausted
from jobdesk.config.loader import load_config
from jobdesk.utils.thread_pool import DaemonThreadPoolExecutor

_LOG = logging.getLogger(__name__)
_TRACER = trace.get_tracer(__name__)

KIND_MATCH = "match"
KIND_COVER_LETTER = "cover_letter"
KIND_RESUME_TAILOR = "resume_tailor"
KIND_INTERVIEW_PREP = "interview_prep"
KIND_IMPORT = "import"
KIND_REEXTRACT = "reextract"
KIND_MATCH_BATCH = "match_batch"
# Пакетные джобы (импорт текстов, переразбор вакансий, пакетная оценка): без вакансии, не
# переиспользуются, мимо interactive-бюджета, квота — в воркере по элементам.
BATCH_KINDS = frozenset({KIND_IMPORT, KIND_REEXTRACT, KIND_MATCH_BATCH})
# «Спросить базу»: вопрос о рынке — без резюме и вакансии, но квота и бюджет как у мэтча.
KIND_ASK_MARKET = "ask_market"
# Без пары резюме–вакансия: активная джоба не переиспользуется (у каждой свои тексты / вопрос).
PAIRLESS_KINDS = BATCH_KINDS | {KIND_ASK_MARKET}
# Все виды — для нулевых серий метрик (observability.init_llm_job_series).
ALL_KINDS = (
    KIND_MATCH,
    KIND_COVER_LETTER,
    KIND_RESUME_TAILOR,
    KIND_INTERVIEW_PREP,
    KIND_IMPORT,
    KIND_REEXTRACT,
    KIND_MATCH_BATCH,
    KIND_ASK_MARKET,
)

# Импорт — до 50 текстов, каждый со своим вызовом LLM: базового таймаута RQ на него мало.
IMPORT_SECONDS_PER_TEXT = 60


def _batch_size(kind: str, params: dict | None) -> int:
    key = "texts" if kind == KIND_IMPORT else "vacancy_ids"
    return len((params or {}).get(key) or [])


def rq_job_timeout(kind: str, params: dict | None) -> int:
    """Таймаут RQ-джобы: базовый LLM_RQ_JOB_TIMEOUT_SECONDS + запас на элементы пакетной джобы."""
    base = get_settings().llm_rq_job_timeout_seconds
    if kind not in BATCH_KINDS:
        return base
    return base + IMPORT_SECONDS_PER_TEXT * _batch_size(kind, params)


class LlmJobUserLimitError(RuntimeError):
    """Пользователь исчерпал лимит параллельных LLM-джоб."""


class LlmJobCancelled(RuntimeError):
    """Владелец отменил выполняемую задачу: воркер записывает ``cancelled``, результат не сохраняется.

    ``model_answered`` — отмена пришла уже после ответа модели: вызов оплачен, квота не
    возвращается. Без ответа модели вызов возвращается в квоту.
    """

    def __init__(self, message: str, *, model_answered: bool = False) -> None:
        super().__init__(message)
        self.model_answered = model_answered


class LlmDailyQuotaError(RuntimeError):
    """Пользователь исчерпал суточную квоту вызовов LLM (``LLM_USER_DAILY_CALLS``)."""


def reserve_user_llm_calls(session, *, user_id: int, calls: int) -> date | None:
    """Списать вызовы из суточной квоты пользователя; админ и квота 0 — без ограничений.

    Квота защищает ключ OpenRouter владельца от остальных зарегистрированных
    пользователей. Списание — в транзакции вызывающего: откат запроса возвращает квоту.
    Возвращает UTC-сутки списания (по ним вызов возвращают), ``None`` — не списано.
    """
    from backend.db.llm_usage_repository import try_reserve_llm_calls
    from backend.db.models import UserRow

    limit = get_settings().llm_user_daily_calls
    if limit <= 0:
        return None
    user = session.get(UserRow, user_id)
    if user is not None and user.is_admin:
        return None
    day = datetime.now(UTC).date()
    if not try_reserve_llm_calls(session, user_id=user_id, calls=calls, daily_limit=limit, day=day):
        raise LlmDailyQuotaError(f"Суточная квота LLM исчерпана ({limit} вызовов в сутки, сброс в 00:00 UTC).")
    return day


@dataclass(frozen=True)
class EnqueueResult:
    job_id: uuid.UUID
    reused: bool


class LlmJobRunner:
    """Постановка LLM-задач: ``inprocess`` или ``rq``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: DaemonThreadPoolExecutor | None = None
        self._max_workers: int | None = None

    def reset(self) -> None:
        """Остановить пул (тесты / shutdown)."""
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            self._max_workers = None

    def _ensure_pool(self, max_workers: int) -> DaemonThreadPoolExecutor:
        with self._lock:
            if self._pool is None or self._max_workers != max_workers:
                if self._pool is not None:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = DaemonThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="llm-job",
                )
                self._max_workers = max_workers
            return self._pool

    def enqueue(
        self,
        *,
        user_id: int,
        kind: str,
        resume_id: int | None = None,
        vacancy_id: str | None = None,
        force: bool = False,
        params: dict | None = None,
    ) -> EnqueueResult:
        """Поставить джобу. Матч / письмо: активная джоба той же пары переиспользуется,
        вызов списывается из суточной квоты сразу. Импорт: не переиспользуется (повтор
        запроса ловит Idempotency-Key), квота списывается в воркере по текстам.
        """
        settings = get_settings()
        backend = settings.llm_job_backend_normalized
        cfg = load_config(settings.config_path)
        per_user = cfg.ai.interactive_per_user_limit
        workers = cfg.ai.interactive_concurrency

        with _TRACER.start_as_current_span(
            "llm_job.enqueue",
            kind=SpanKind.PRODUCER,
            attributes={
                "llm_job.kind": kind,
                "llm_job.backend": backend,
                "messaging.system": "rq" if backend == "rq" else "inprocess",
                "messaging.destination": "llm",
            },
        ) as span:
            with session_scope() as session:
                lock_user_llm_enqueue(session, user_id=user_id)
                existing = None
                if kind not in PAIRLESS_KINDS:
                    existing = repo.find_active_llm_job(
                        session,
                        user_id=user_id,
                        kind=kind,
                        resume_id=resume_id,
                        vacancy_id=vacancy_id,
                    )
                if existing is not None:
                    span.set_attribute("llm_job.id", str(existing.id))
                    record_llm_job_enqueue(backend=backend, kind=kind, outcome="reused")
                    return EnqueueResult(job_id=existing.id, reused=True)

                active = repo.count_active_llm_jobs(session, user_id=user_id)
                if active >= per_user:
                    record_llm_job_enqueue(backend=backend, kind=kind, outcome="rejected_limit")
                    raise LlmJobUserLimitError(
                        f"Слишком много параллельных LLM-задач (лимит {per_user}). Дождитесь завершения."
                    )
                quota_day = None
                budget = cfg.ai.interactive_daily_request_budget
                if kind not in BATCH_KINDS and interactive_daily_budget_exhausted(budget):
                    record_llm_job_enqueue(backend=backend, kind=kind, outcome="rejected_budget")
                    raise LlmDailyQuotaError(
                        f"Общий суточный бюджет LLM исчерпан ({budget} запросов в сутки): "
                        "оценки и письма недоступны до 00:00 UTC."
                    )
                if kind not in BATCH_KINDS:
                    try:
                        quota_day = reserve_user_llm_calls(session, user_id=user_id, calls=1)
                    except LlmDailyQuotaError:
                        record_llm_job_enqueue(backend=backend, kind=kind, outcome="rejected_quota")
                        raise

                row = repo.create_llm_job(
                    session,
                    user_id=user_id,
                    kind=kind,
                    resume_id=resume_id,
                    vacancy_id=vacancy_id,
                    force=force,
                    params=params,
                )
                row.quota_day = quota_day
                job_id = row.id

            span.set_attribute("llm_job.id", str(job_id))
            record_llm_job(outcome="queued", kind=kind)
            try:
                self._dispatch(job_id, backend=backend, pool_workers=workers, timeout=rq_job_timeout(kind, params))
            except Exception:
                record_llm_job_enqueue(backend=backend, kind=kind, outcome="error")
                with session_scope() as session:
                    repo.set_llm_job_status(
                        session,
                        job_id,
                        "error",
                        error_message="Failed to enqueue job",
                    )
                    refund_llm_job_quota(session, job_id)
                raise
            record_llm_job_enqueue(backend=backend, kind=kind, outcome="queued")
            _LOG.info(
                "LLM job queued job_id=%s kind=%s backend=%s",
                job_id,
                kind,
                backend,
            )
            return EnqueueResult(job_id=job_id, reused=False)

    def _dispatch(self, job_id: uuid.UUID, *, backend: str, pool_workers: int, timeout: int) -> None:
        from backend.workers.llm_tasks import run_llm_job

        traceparent = current_traceparent()
        if backend == "rq":
            from backend.queue.rq_conn import get_llm_queue

            q = get_llm_queue()
            rq_job = q.enqueue(
                run_llm_job,
                str(job_id),
                traceparent,
                job_timeout=timeout,
                result_ttl=3600,
                failure_ttl=86400,
            )
            with session_scope() as session:
                repo.set_llm_job_rq_job_id(session, job_id, str(rq_job.id))
            _LOG.info("RQ enqueue job_id=%s rq_job_id=%s", job_id, rq_job.id)
            return

        pool = self._ensure_pool(pool_workers)
        pool.submit(run_llm_job, str(job_id), traceparent)


llm_job_runner = LlmJobRunner()


def cancel_llm_job(job_id: uuid.UUID, *, user_id: int) -> bool:
    """Отмена задачи владельцем: ``queued`` — сразу, ``running`` — флагом для воркера.

    False — задачи нет или она чужая. Завершённая задача не меняется (идемпотентно).
    """
    from backend.db import llm_job_repository as llm_jobs
    from backend.services.llm_job_events import publish_llm_job_event

    with session_scope() as session:
        row = llm_jobs.get_llm_job(session, job_id)
        if row is None or row.user_id != user_id:
            return False
        was_queued = row.status == "queued"
        row = llm_jobs.request_llm_job_cancel(session, job_id)
        status, kind, rq_job_id = row.status, row.kind, row.rq_job_id
    if was_queued and status == "cancelled":
        if rq_job_id:
            try:
                from rq.job import Job

                from backend.queue.rq_conn import get_redis_connection

                Job.fetch(rq_job_id, connection=get_redis_connection()).cancel()
            except Exception as e:
                # Не критично: воркер не возьмёт задачу — claim требует queued.
                _LOG.debug("LLM job %s: RQ job не снята: %s", job_id, e)
        publish_llm_job_event(job_id, status="cancelled", kind=kind)
        record_llm_job(outcome="cancelled", kind=kind)
    _LOG.info("LLM job %s: запрошена отмена (status=%s)", job_id, status)
    return True
