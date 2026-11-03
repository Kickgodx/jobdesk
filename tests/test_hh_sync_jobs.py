"""HH sync как durable-джоба: recover по истёкшему lease, лимит попыток, fencing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import clear_settings_caches

from jobdesk_hh import job_service as hh_sync_job_service
from jobdesk_hh import tasks as hh_sync_tasks
from jobdesk_hh.db import jobs
from jobdesk_hh.db.models import HhSyncJobRow
from jobdesk_hh.db.session import reset_database, session_scope
from jobdesk_hh.recover import recover_stuck_hh_sync_jobs


@pytest.fixture
def hh_db(pg_db, monkeypatch):
    """Схема hh тестовой базы; HH-сервис читает DATABASE_URL заново."""
    monkeypatch.setenv("HH_SYNC_JOB_BACKEND", "inprocess")
    clear_settings_caches()
    reset_database()
    yield pg_db
    reset_database()
    clear_settings_caches()


@pytest.fixture
def db(hh_db, monkeypatch):
    """Постановку в очередь записываем вместо запуска."""
    enqueued: list = []
    monkeypatch.setattr(
        hh_sync_job_service, "enqueue_hh_sync_job", lambda job_id, traceparent=None: enqueued.append(job_id)
    )
    return enqueued


def _running_job(*, attempts: int, lease_until: datetime) -> tuple:
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "7d", "period": 7}, requested_by=None).id
    owner = jobs.new_lease_owner()
    with session_scope() as session:
        assert jobs.claim_hh_sync_job(session, job_id, lease_owner=owner) is not None
        row = session.get(HhSyncJobRow, job_id)
        row.attempts = attempts
        row.lease_until = lease_until
    return job_id, owner


def _row(job_id) -> HhSyncJobRow:
    with session_scope() as session:
        row = session.get(HhSyncJobRow, job_id)
        session.expunge(row)
        return row


def test_recover_requeues_running_job_with_expired_lease(db):
    job_id, _ = _running_job(attempts=1, lease_until=datetime.now(UTC) - timedelta(minutes=1))

    assert recover_stuck_hh_sync_jobs() == 1

    row = _row(job_id)
    assert row.status == "queued"
    assert row.lease_owner is None
    assert db == [job_id]


def test_recover_leaves_live_lease_alone(db):
    job_id, _ = _running_job(attempts=1, lease_until=datetime.now(UTC) + timedelta(minutes=1))

    assert recover_stuck_hh_sync_jobs() == 0
    assert _row(job_id).status == "running"
    assert db == []


def test_recover_fails_job_after_max_attempts(db, monkeypatch):
    monkeypatch.setenv("HH_SYNC_JOB_MAX_ATTEMPTS", "2")
    clear_settings_caches()
    job_id, _ = _running_job(attempts=2, lease_until=datetime.now(UTC) - timedelta(minutes=1))

    assert recover_stuck_hh_sync_jobs() == 0

    row = _row(job_id)
    assert row.status == "failed"
    assert "Превышено число попыток (2)" in (row.error_message or "")
    assert db == []
    # Слот активного sync освобождён.
    with session_scope() as session:
        assert jobs.has_active_hh_sync_job(session) is False


def test_recover_requeues_stale_queued_on_start(db):
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None).id

    assert recover_stuck_hh_sync_jobs() == 0  # свежий queued — ещё не stuck
    assert recover_stuck_hh_sync_jobs(requeue_all_queued=True) == 1
    assert db == [job_id]


def test_job_stops_without_finalizing_when_lease_is_lost(db, monkeypatch):
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None).id

    def fake_sync(session, *, config, settings, search_window, progress_callback, **_kwargs):
        progress_callback("search", hh_sync_tasks.HhSyncResult(), 0, 0)
        raise AssertionError("sync должен остановиться на потере lease")

    monkeypatch.setattr(hh_sync_tasks, "run_hh_sync", fake_sync)
    monkeypatch.setattr(hh_sync_tasks.jobs, "patch_hh_sync_job_progress", lambda *a, **k: False)

    hh_sync_tasks.run_hh_sync_job(str(job_id))

    # Финализирует новый владелец lease (после recover), а не этот воркер.
    row = _row(job_id)
    assert row.status == "running"
    assert row.finished_at is None


def test_job_records_result_and_progress(db, monkeypatch):
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "7d", "period": 7}, requested_by=None).id
    seen = {}

    def fake_sync(session, *, config, settings, search_window, progress_callback, **_kwargs):
        seen["window"] = search_window
        result = hh_sync_tasks.HhSyncResult(searched=3, unique=2, window="7d", period=7)
        progress_callback("details", result, 1, 2)
        progress_callback("purge", result, 2, 2)
        return result

    monkeypatch.setattr(hh_sync_tasks, "run_hh_sync", fake_sync)

    hh_sync_tasks.run_hh_sync_job(str(job_id))

    row = _row(job_id)
    assert row.status == "completed"
    assert row.result["searched"] == 3
    assert row.progress["phase"] == "purge"
    assert row.progress["done"] == 2
    assert row.lease_owner is None
    assert seen["window"].period == 7


def test_unexpected_error_fails_job_with_short_message(db, monkeypatch):
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None).id

    def boom(*args, **kwargs):
        raise ValueError("x" * 2000)

    monkeypatch.setattr(hh_sync_tasks, "run_hh_sync", boom)

    hh_sync_tasks.run_hh_sync_job(str(job_id))

    row = _row(job_id)
    assert row.status == "failed"
    assert row.error_message.startswith("ValueError: xxx")
    assert len(row.error_message) <= 500


def test_enqueue_failure_marks_job_failed(hh_db, monkeypatch):
    from jobdesk_hh.window import ResolvedHhSearchWindow

    def broken(job_id, traceparent=None):
        raise ConnectionError("redis down")

    monkeypatch.setattr(hh_sync_job_service, "enqueue_hh_sync_job", broken)

    with pytest.raises(ConnectionError):
        hh_sync_job_service.start_hh_sync_job(ResolvedHhSearchWindow(window="all"), requested_by=None)

    with session_scope() as session:
        (row,) = jobs.list_hh_sync_jobs(session)
        assert row.status == "failed"
        assert row.error_message == "Failed to enqueue job"
    # Следующий запуск не упирается в 409.
    monkeypatch.setattr(hh_sync_job_service, "enqueue_hh_sync_job", lambda job_id, traceparent=None: None)
    hh_sync_job_service.start_hh_sync_job(ResolvedHhSearchWindow(window="all"), requested_by=None)


# --- Отмена sync ---


def test_cancel_queued_sync_is_immediate_and_frees_slot(db):
    with session_scope() as session:
        job_id = jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None).id
        row = jobs.request_hh_sync_job_cancel(session, job_id)
        assert (row.status, row.cancel_requested) == ("cancelled", True)
        assert jobs.has_active_hh_sync_job(session) is False
        # Новый sync ставится сразу.
        jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None)
    with session_scope() as session:
        assert jobs.claim_hh_sync_job(session, job_id, lease_owner=jobs.new_lease_owner()) is None


def test_cancel_running_sync_sets_flag(db):
    job_id, _ = _running_job(attempts=1, lease_until=datetime.now(UTC) + timedelta(minutes=1))
    with session_scope() as session:
        row = jobs.request_hh_sync_job_cancel(session, job_id)
        assert (row.status, row.cancel_requested) == ("running", True)
        assert jobs.is_hh_sync_job_cancel_requested(session, job_id) is True


def test_recover_cancels_stuck_sync_with_cancel_requested(db):
    job_id, _ = _running_job(attempts=1, lease_until=datetime.now(UTC) - timedelta(minutes=1))
    with session_scope() as session:
        jobs.request_hh_sync_job_cancel(session, job_id)

    assert recover_stuck_hh_sync_jobs() == 0

    row = _row(job_id)
    assert (row.status, row.lease_owner) == ("cancelled", None)
    assert db == []
