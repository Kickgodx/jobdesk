"""HTTP API HH-сервиса напрямую (без прокси ядра): токен, health, ошибки, старт в inprocess."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from tests.conftest import clear_settings_caches

from jobdesk.contracts.hh_service import SYNC_JOBS_PATH, SYNC_PATH, USER_ID_HEADER, sync_job_path
from jobdesk_hh.db import jobs
from jobdesk_hh.db.session import reset_database, session_scope
from jobdesk_hh.settings import get_hh_settings


@pytest.fixture
def hh_app(pg_db, monkeypatch, default_test_config):
    monkeypatch.setenv("CONFIG_PATH", str(default_test_config))
    monkeypatch.setenv("HH_SYNC_JOB_BACKEND", "inprocess")
    monkeypatch.setenv("HH_SERVICE_METRICS_PORT", "0")
    monkeypatch.setenv("HH_MAINTENANCE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HH_API_TOKEN", "")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "")
    monkeypatch.setenv("REDIS_URL", "")
    clear_settings_caches()
    reset_database()
    from jobdesk_hh.api import app

    yield app
    reset_database()
    clear_settings_caches()


def _auth() -> dict:
    return {"Authorization": f"Bearer {get_hh_settings().hh_service_token}"}


def test_health_is_open_and_ok(hh_app):
    with TestClient(hh_app) as c:
        assert c.get("/health").json() == {"status": "ok", "database": True}


def test_health_degraded_without_database(hh_app, monkeypatch):
    with TestClient(hh_app) as c:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tgjobs:x@127.0.0.1:1/tgjobs?connect_timeout=1")
        clear_settings_caches()
        reset_database()
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "database": False}


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Bearer " + "x" * 40])
def test_v1_requires_service_token(hh_app, auth):
    with TestClient(hh_app) as c:
        resp = c.get(SYNC_JOBS_PATH, headers={"Authorization": auth} if auth else {})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "NOT_AUTHENTICATED"


def test_window_error_is_422_and_not_configured_is_503(hh_app):
    with TestClient(hh_app) as c:
        bad = c.post(SYNC_PATH, json={"window": "range"}, headers=_auth())
        assert bad.status_code == 422
        assert bad.json()["detail"]["code"] == "VALIDATION"
        off = c.post(SYNC_PATH, json={"window": "7d"}, headers=_auth())
        assert off.status_code == 503
        assert off.json()["detail"] == {
            "code": "UPSTREAM_UNAVAILABLE",
            "message": "HH_API_TOKEN is not set",
            "retryable": True,
        }
        with session_scope() as s:
            assert jobs.list_hh_sync_jobs(s) == []


def test_unknown_job_is_404(hh_app):
    with TestClient(hh_app) as c:
        resp = c.get(sync_job_path("00000000-0000-0000-0000-000000000000"), headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"


def test_requested_by_comes_from_user_header(hh_app, monkeypatch):
    from jobdesk_hh import job_service

    monkeypatch.setenv("HH_API_TOKEN", "t")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "a@b.c")
    clear_settings_caches()
    monkeypatch.setattr(job_service, "enqueue_hh_sync_job", lambda job_id, traceparent=None: None)
    with TestClient(hh_app) as c:
        resp = c.post(SYNC_PATH, json={}, headers={**_auth(), USER_ID_HEADER: "42"})
        assert resp.status_code == 202, resp.text
    with session_scope() as s:
        (row,) = jobs.list_hh_sync_jobs(s)
        assert row.requested_by == 42


def test_inprocess_start_runs_leftover_queued_sync(hh_app, monkeypatch):
    """Рестарт сервиса в inprocess: queued прошлого процесса ставится в работу сразу (durable-jobs)."""
    from jobdesk_hh import tasks

    def fake_sync(session, *, search_window, **_kwargs):
        return tasks.HhSyncResult(window=search_window.window)

    monkeypatch.setattr(tasks, "run_hh_sync", fake_sync)
    with session_scope() as s:
        job_id = jobs.create_hh_sync_job(s, params={"window": "all"}, requested_by=None).id

    with TestClient(hh_app):
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            with session_scope() as s:
                status = jobs.get_hh_sync_job(s, job_id).status
            if status == "completed":
                break
            time.sleep(0.05)
    assert status == "completed"
