"""RQ-воркер HH-сервиса целиком: ``jobdesk_hh.worker.main()`` → платформенный раннер → fakeredis.

Сервис ставит sync в очередь ``hh`` (бэкенд ``rq``), воркер забирает и выполняет его.
Побочные эффекты процесса (логирование, .env, порт /metrics, watchdog) подменены; ``work()``
идёт в режиме ``burst``.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import fakeredis
import pytest
from rq import Queue
from tests.conftest import clear_settings_caches

from jobdesk_hh import job_service, queue, tasks, worker
from jobdesk_hh.db import jobs
from jobdesk_hh.db.session import reset_database, session_scope
from jobdesk_hh.settings import get_hh_settings
from jobdesk_hh.window import ResolvedHhSearchWindow
from jobdesk_platform.workers import liveness


@pytest.fixture
def fake_redis(pg_db, monkeypatch):
    server = fakeredis.FakeStrictRedis()
    monkeypatch.setenv("HH_SYNC_JOB_BACKEND", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    clear_settings_caches()
    reset_database()
    queue.reset_redis_connection()
    monkeypatch.setattr(queue, "get_redis_connection", lambda: server)
    yield server
    queue.reset_redis_connection()
    reset_database()
    clear_settings_caches()


@pytest.fixture
def worker_process(monkeypatch):
    rec = SimpleNamespace(metrics_ports=[], recover_interval=None, watchdog_seconds=None, queues=None)

    def start_periodic_recover(fn, *, interval_seconds, name):
        rec.recover_interval = interval_seconds
        return threading.Event()

    def start_watchdog(stall_seconds, *, name):
        rec.watchdog_seconds = stall_seconds
        return None

    original_work = liveness.LivenessWorker.work

    def burst_work(self, *args, **kwargs):
        rec.queues = self.queue_names()
        return original_work(self, burst=True, with_scheduler=False)

    monkeypatch.setattr("jobdesk.utils.logger.setup_logging", lambda: None)
    monkeypatch.setattr("jobdesk.utils.env_loader.try_load_dotenv", lambda: None)
    monkeypatch.setattr("prometheus_client.start_http_server", lambda port: rec.metrics_ports.append(port))
    monkeypatch.setattr("jobdesk_platform.workers.recover_loop.start_periodic_recover", start_periodic_recover)
    monkeypatch.setattr(liveness, "start_watchdog", start_watchdog)
    monkeypatch.setattr(liveness.LivenessWorker, "work", burst_work)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "x")
    monkeypatch.delenv("OTEL_SERVICE_NAME")
    return rec


def test_hh_worker_runs_queued_sync_job(fake_redis, worker_process, monkeypatch):
    seen: dict = {}

    def fake_sync(session, *, config, settings, core, search_window, progress_callback, **_kwargs):
        seen["window"] = search_window
        result = tasks.HhSyncResult(searched=4, unique=3, window="7d", period=7)
        progress_callback("purge", result, 1, 1)
        return result

    monkeypatch.setattr(tasks, "run_hh_sync", fake_sync)
    job_id = job_service.start_hh_sync_job(ResolvedHhSearchWindow(window="7d", period=7), requested_by=7)
    assert Queue(queue.HH_QUEUE_NAME, connection=fake_redis).count == 1

    worker.main()

    with session_scope() as session:
        row = jobs.get_hh_sync_job(session, job_id)
        assert row.status == "completed", row.error_message
        assert row.result["searched"] == 4
        assert row.requested_by == 7
    assert seen["window"].period == 7
    assert Queue(queue.HH_QUEUE_NAME, connection=fake_redis).count == 0
    settings = get_hh_settings()
    assert worker_process.queues == [queue.HH_QUEUE_NAME]
    assert worker_process.metrics_ports == [settings.hh_worker_metrics_port]
    assert worker_process.recover_interval == settings.job_recover_interval_seconds
    assert worker_process.watchdog_seconds == settings.worker_stall_exit_seconds
    assert os.environ["OTEL_SERVICE_NAME"] == "jobdesk-hh-worker"


def test_hh_worker_requires_redis_url(fake_redis, worker_process, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    clear_settings_caches()
    with pytest.raises(SystemExit):
        worker.main()
