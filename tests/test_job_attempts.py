"""Счётчик попыток: отравленная джоба не requeue'ится бесконечно; recover в API для inprocess."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.db import llm_job_repository as lj
from backend.db import parse_run_repository as pr
from backend.db.models import LlmJobRow, ResumeRow, UserRow, VacancyRow
from backend.db.session import reset_engine, session_scope
from backend.settings import get_settings
from backend.workers import parse_tasks
from backend.workers.recover import recover_stuck_llm_jobs
from backend.workers.recover_parse import recover_stuck_parse_runs
from jobdesk.config.loader import load_config
from jobdesk.models.metrics import MetricsSummary
from jobdesk.models.parse_result import ParseResult


@pytest.fixture(autouse=True)
def _db(monkeypatch, pg_db):
    monkeypatch.setenv("JWT_SECRET", "test-secret-value-for-tests")
    monkeypatch.setenv("PARSE_JOB_BACKEND", "inprocess")
    monkeypatch.setenv("LLM_JOB_BACKEND", "inprocess")
    monkeypatch.setenv("PARSE_JOB_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LLM_JOB_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    reset_engine()
    yield
    reset_engine()
    get_settings.cache_clear()


def _expire_parse_lease(run_id: uuid.UUID) -> None:
    with session_scope() as session:
        row = pr.get_parse_run(session, run_id)
        assert row is not None and row.status == "running"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=5)


def _crash_parse_attempt(run_id: uuid.UUID) -> str | None:
    """Воркер взял прогон и умер: claim → lease истёк → reclaim."""
    with session_scope() as session:
        assert pr.claim_parse_run(session, run_id) is not None
    _expire_parse_lease(run_id)
    with session_scope() as session:
        return pr.reclaim_expired_parse_run(session, run_id)


def _llm_job() -> uuid.UUID:
    with session_scope() as session:
        user = UserRow(email="attempts@test", password_hash="x", is_active=True)
        session.add(user)
        session.flush()
        resume = ResumeRow(user_id=user.id, name="t", text="body")
        session.add(resume)
        session.add(
            VacancyRow(
                id="v-attempts-1",
                source_post_key="ch-v-attempts-1",
                source_channel="ch",
                source_url="https://t.me/s/ch",
                post_date=datetime(2026, 9, 1, tzinfo=UTC),
                raw_text="Python",
                cleaned_text="Python",
            )
        )
        session.flush()
        job = LlmJobRow(
            user_id=user.id,
            kind="match",
            status="queued",
            resume_id=resume.id,
            vacancy_id="v-attempts-1",
            params={},
        )
        session.add(job)
        session.flush()
        return job.id


def _crash_llm_attempt(job_id: uuid.UUID) -> None:
    with session_scope() as session:
        claimed = lj.claim_llm_job(session, job_id)
        assert claimed is not None
        claimed.lease_until = datetime.now(UTC) - timedelta(seconds=5)


def test_claim_increments_attempts():
    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot={}).id
        assert pr.get_parse_run(session, rid).attempts == 0
    with session_scope() as session:
        assert pr.claim_parse_run(session, rid).attempts == 1


def test_parse_run_fails_after_max_attempts():
    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot={}).id

    assert _crash_parse_attempt(rid) == "queued"
    assert _crash_parse_attempt(rid) == "queued"
    assert _crash_parse_attempt(rid) == "failed"  # третья попытка — последняя

    with session_scope() as session:
        row = pr.get_parse_run(session, rid)
        assert row.status == "failed"
        assert row.attempts == 3
        assert "Превышено число попыток (3)" in (row.error_message or "")
        assert row.finished_at is not None
        assert row.lease_owner is None
        # Новый запуск больше не упирается в 409 CONFLICT_ACTIVE_RUN.
        assert pr.has_active_parse_run(session) is False


def test_cancel_requested_wins_over_attempt_limit():
    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot={}).id
    assert _crash_parse_attempt(rid) == "queued"
    assert _crash_parse_attempt(rid) == "queued"
    with session_scope() as session:
        assert pr.claim_parse_run(session, rid) is not None
        pr.request_parse_run_cancel(session, rid)
    _expire_parse_lease(rid)
    with session_scope() as session:
        assert pr.reclaim_expired_parse_run(session, rid) == "cancelled"


def test_recover_does_not_requeue_poisoned_parse_run(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(parse_tasks, "run_parse_job", lambda run_id, traceparent=None: started.append(run_id))

    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot={}).id
    assert _crash_parse_attempt(rid) == "queued"
    assert _crash_parse_attempt(rid) == "queued"
    with session_scope() as session:
        assert pr.claim_parse_run(session, rid) is not None
    _expire_parse_lease(rid)

    assert recover_stuck_parse_runs() == 0
    assert started == []
    with session_scope() as session:
        assert pr.get_parse_run(session, rid).status == "failed"


def test_llm_job_errors_after_max_attempts():
    jid = _llm_job()

    _crash_llm_attempt(jid)
    with session_scope() as session:
        assert lj.reclaim_expired_llm_job(session, jid) == "queued"

    _crash_llm_attempt(jid)
    with session_scope() as session:
        assert lj.reclaim_expired_llm_job(session, jid) == "error"
        row = lj.get_llm_job(session, jid)
        assert row.status == "error"
        assert row.attempts == 2
        assert "Превышено число попыток (2)" in (row.error_message or "")
        # Джоба больше не занимает лимит параллельных задач пользователя.
        assert lj.count_active_llm_jobs(session, user_id=row.user_id) == 0


def test_recover_does_not_requeue_poisoned_llm_job():
    jid = _llm_job()
    _crash_llm_attempt(jid)
    with session_scope() as session:
        assert lj.reclaim_expired_llm_job(session, jid) == "queued"
    _crash_llm_attempt(jid)

    assert recover_stuck_llm_jobs() == 0
    with session_scope() as session:
        assert lj.get_llm_job(session, jid).status == "error"


def test_recover_cancels_stuck_llm_job_with_cancel_requested():
    """Отмена запрошена, воркер пропал: recover закрывает задачу cancelled, а не ставит на повтор."""
    jid = _llm_job()
    with session_scope() as session:
        assert lj.claim_llm_job(session, jid, lease_owner="dead-worker") is not None
        lj.request_llm_job_cancel(session, jid)
    with session_scope() as session:
        session.get(lj.LlmJobRow, jid).lease_until = datetime.now(UTC) - timedelta(minutes=1)

    assert recover_stuck_llm_jobs() == 0
    with session_scope() as session:
        row = lj.get_llm_job(session, jid)
        assert (row.status, row.lease_owner) == ("cancelled", None)
        assert row.finished_at is not None


def _wait_for_status(run_id: uuid.UUID, expected: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        with session_scope() as session:
            status = pr.get_parse_run(session, run_id).status
        if status == expected:
            break
        time.sleep(0.05)
    return status


def _empty_result(config, **kwargs) -> ParseResult:
    return ParseResult(
        run_params={},
        posts_fetched=0,
        vacancies_all=[],
        vacancies_unique=[],
        parse_warnings="",
        metrics=MetricsSummary(),
    )


def test_api_start_recovers_queued_run_inprocess(monkeypatch, default_test_config):
    """Рестарт API в inprocess: ``queued`` без потока поднимается сразу, не через stuck-таймаут."""
    from backend.main import _start_inprocess_job_recovery

    monkeypatch.setattr(parse_tasks, "run_parse", _empty_result)
    snapshot = load_config(default_test_config).model_dump(mode="json")
    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot=snapshot).id

    stops = _start_inprocess_job_recovery()
    try:
        assert len(stops) == 2  # parse + llm; HH sync восстанавливает HH-сервис
        assert _wait_for_status(rid, "completed") == "completed"
    finally:
        for stop in stops:
            stop.set()


def test_api_recovers_running_run_after_lease_expiry(monkeypatch, default_test_config):
    """``running`` от прошлого процесса: после истечения lease recover перезапускает прогон."""
    monkeypatch.setattr(parse_tasks, "run_parse", _empty_result)
    snapshot = load_config(default_test_config).model_dump(mode="json")
    with session_scope() as session:
        rid = pr.create_parse_run(session, config_snapshot=snapshot).id
    with session_scope() as session:
        assert pr.claim_parse_run(session, rid) is not None

    assert recover_stuck_parse_runs(requeue_all_queued=True) == 0  # lease ещё жив
    _expire_parse_lease(rid)
    assert recover_stuck_parse_runs() == 1

    assert _wait_for_status(rid, "completed") == "completed"
    with session_scope() as session:
        assert pr.get_parse_run(session, rid).attempts == 2


def test_api_start_skips_recovery_for_rq_backends(monkeypatch):
    from backend.main import _start_inprocess_job_recovery

    monkeypatch.setenv("PARSE_JOB_BACKEND", "rq")
    monkeypatch.setenv("LLM_JOB_BACKEND", "rq")
    monkeypatch.setenv("HH_SYNC_JOB_BACKEND", "rq")
    get_settings.cache_clear()

    assert _start_inprocess_job_recovery() == []
