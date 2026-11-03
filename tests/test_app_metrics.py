"""Тесты доменных Prometheus-метрик (backend.observability)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prometheus_client import REGISTRY
from tests.conftest import metrics_text
from tests.llm_job_helpers import await_llm_job

from backend.db import repository as repo
from backend.db.session import get_session_factory
from backend.services import match_service
from jobdesk.models.vacancy import Vacancy
from jobdesk.utils.hashing import vacancy_content_id

VACANCY_TEXT = (
    "Вакансия: Senior Python-разработчик в компанию Acme.\n"
    "Зарплата: от 250 000 до 350 000 руб. на руки.\n"
    "Формат: удалёнка. Нужен опыт с PostgreSQL и Docker.\n"
    "Контакт: @recruiter_acme"
)


def _sample(name: str, labels: dict[str, str]) -> float:
    for family in REGISTRY.collect():
        for s in family.samples:
            if s.name == name and s.labels == labels:
                return float(s.value)
    return 0.0


def _has_sample(name: str, labels: dict[str, str]) -> bool:
    return any(s.name == name and s.labels == labels for family in REGISTRY.collect() for s in family.samples)


def _register(client, email: str, password: str = "secret1") -> str:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _vacancy(text: str, *, post_date: datetime | None = None) -> Vacancy:
    return Vacancy(
        id=vacancy_content_id(text),
        source_post_key="ch-1",
        source_channel="ch",
        source_url="https://t.me/s/ch",
        post_date=post_date,
        raw_text=text,
        cleaned_text=text,
        detected_specializations=["Python"],
    )


def test_auth_events_metric(api_client):
    before_reg = _sample("auth_events_total", {"event": "register", "outcome": "success"})
    token = _register(api_client, "m@example.com")
    assert _sample("auth_events_total", {"event": "register", "outcome": "success"}) == before_reg + 1

    before_invalid = _sample("auth_events_total", {"event": "login", "outcome": "invalid"})
    r = api_client.post(
        "/api/v1/auth/login",
        json={"email": "m@example.com", "password": "wrong_password"},
    )
    assert r.status_code == 401
    assert _sample("auth_events_total", {"event": "login", "outcome": "invalid"}) == before_invalid + 1

    before_ok = _sample("auth_events_total", {"event": "login", "outcome": "success"})
    r = api_client.post("/api/v1/auth/login", json={"email": "m@example.com", "password": "secret1"})
    assert r.status_code == 200
    assert token
    assert _sample("auth_events_total", {"event": "login", "outcome": "success"}) == before_ok + 1


def test_import_items_metric(api_client):
    token = _register(api_client, "imp@example.com")  # первый — admin: импорт admin-only

    before_created = _sample("vacancy_import_items_total", {"status": "created"})
    r = api_client.post("/api/v1/vacancies/import", headers=_auth(token), json={"texts": [VACANCY_TEXT]})
    assert r.status_code == 200, r.text
    assert _sample("vacancy_import_items_total", {"status": "created"}) == before_created + 1

    before_exists = _sample("vacancy_import_items_total", {"status": "exists"})
    r = api_client.post("/api/v1/vacancies/import", headers=_auth(token), json={"texts": [VACANCY_TEXT]})
    assert r.json()["items"][0]["status"] == "exists"
    assert _sample("vacancy_import_items_total", {"status": "exists"}) == before_exists + 1


def test_match_metrics(api_client, monkeypatch):
    from jobdesk.ai.clients.openrouter import LLMUsage
    from jobdesk.ai.schema import MatchBreakdown, VacancyMatchExtraction

    class _FakeClient:
        def match_with_fallback(self, *, resume_text, vacancy_text):
            return (
                VacancyMatchExtraction(
                    confidence=88,
                    breakdown=MatchBreakdown(
                        role_match=90,
                        skills_match=70,
                        seniority_match=80,
                        domain_match=100,
                        work_format_match=100,
                    ),
                ),
                LLMUsage(model="m1"),
            )

    monkeypatch.setattr(match_service, "get_interactive_client", lambda: _FakeClient())

    token = _register(api_client, "mm@example.com")
    v = _vacancy(VACANCY_TEXT, post_date=datetime(2026, 9, 1, tzinfo=UTC))
    factory = get_session_factory()
    session = factory()
    try:
        repo.upsert_vacancies(session, [v], parse_run_id=None)
        session.commit()
    finally:
        session.close()
    rid = api_client.post("/api/v1/resumes", headers=_auth(token), json={"name": "CV", "text": "python"}).json()["id"]

    before_ok = _sample("vacancy_match_calls_total", {"outcome": "success"})
    before_score = _sample("vacancy_match_score_count", {})
    before_jobs_done = _sample("llm_jobs_total", {"outcome": "done", "kind": "match"})
    r = api_client.post(f"/api/v1/resumes/{rid}/match/{v.id}", headers=_auth(token))
    job = await_llm_job(api_client, token, r)
    assert job["status"] == "done", job
    assert _sample("vacancy_match_calls_total", {"outcome": "success"}) == before_ok + 1
    assert _sample("vacancy_match_score_count", {}) == before_score + 1
    assert _sample("llm_jobs_total", {"outcome": "done", "kind": "match"}) == before_jobs_done + 1

    before_cached = _sample("vacancy_match_calls_total", {"outcome": "cached"})
    r = api_client.post(f"/api/v1/resumes/{rid}/match/{v.id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["cached"] is True
    assert _sample("vacancy_match_calls_total", {"outcome": "cached"}) == before_cached + 1


def test_upsert_and_archive_metrics(api_client):
    old_date = datetime.now(UTC) - timedelta(days=365)
    v = _vacancy("Старая вакансия про Python", post_date=old_date)

    before_ins = _sample("vacancies_upserted_total", {"result": "inserted"})
    factory = get_session_factory()
    session = factory()
    try:
        stats = repo.upsert_vacancies(session, [v], parse_run_id=None)
        session.commit()
    finally:
        session.close()
    assert stats.inserted == 1
    assert _sample("vacancies_upserted_total", {"result": "inserted"}) == before_ins + 1

    before_arch = _sample("vacancies_archived_total", {})
    session = factory()
    try:
        assert repo.archive_expired_vacancies(session) == 1
        session.commit()
    finally:
        session.close()
    assert _sample("vacancies_archived_total", {}) == before_arch + 1


def test_db_stats_collector_in_metrics(api_client):
    _register(api_client, "db@example.com")
    body = metrics_text()
    for name in (
        "db_vacancies",
        "db_resumes",
        "db_users",
        "db_vacancy_matches",
        "db_ai_cache_entries",
        "db_parse_runs",
        "db_parse_runs_finished_24h",
        "db_parse_posts_fetched_24h",
    ):
        assert name in body
    assert "db_users} 1.0" in body or "db_users 1.0" in body


def test_db_pool_collector_queue_pool(monkeypatch):
    """На QueuePool отдаём занято/свободно/лимит; не-QueuePool — пустой scrape."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool, QueuePool

    from backend.observability import _DBPoolCollector

    # Без соединения: коллектор читает только счётчики пула.
    url = "postgresql+psycopg://u:p@127.0.0.1:1/x"
    eng = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
    )
    monkeypatch.setattr("backend.db.session.get_engine", lambda: eng)

    by_name = {fam.name: fam for fam in _DBPoolCollector().collect()}
    assert by_name["db_pool_size"].samples[0].value == 3.0
    assert by_name["db_pool_max_overflow"].samples[0].value == 2.0
    assert by_name["db_pool_max_connections"].samples[0].value == 5.0
    assert by_name["db_pool_checked_out"].samples[0].value == 0.0
    assert by_name["db_pool_checked_in"].samples[0].value == 0.0
    assert by_name["db_pool_overflow"].samples[0].value == 0.0

    null_eng = create_engine(url, poolclass=NullPool)
    monkeypatch.setattr("backend.db.session.get_engine", lambda: null_eng)
    assert list(_DBPoolCollector().collect()) == []


def test_db_parse_runs_finished_24h_counts_recent(api_client):
    factory = get_session_factory()
    session = factory()
    try:
        from backend.db.models import ParseRunRow

        now = datetime.now(UTC)
        session.add(
            ParseRunRow(
                status="completed",
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=30),
                posts_fetched=12,
            )
        )
        session.add(
            ParseRunRow(
                status="failed",
                started_at=now - timedelta(days=2),
                finished_at=now - timedelta(days=2) + timedelta(minutes=5),
                posts_fetched=3,
            )
        )
        session.commit()
    finally:
        session.close()

    assert _sample("db_parse_runs_finished_24h", {"status": "completed"}) >= 1
    # Упавший прогон — двое суток назад: серия есть, но с нулём (иначе stat-панель — No data).
    assert _has_sample("db_parse_runs_finished_24h", {"status": "failed"})
    assert _sample("db_parse_runs_finished_24h", {"status": "failed"}) == 0
    assert _sample("db_parse_posts_fetched_24h", {}) >= 12


def test_db_jobs_lease_expired_counts_running_with_stale_lease(api_client):
    from tests.conftest import new_user

    from backend.db.models import HhSyncJobRow, LlmJobRow, ParseRunRow

    now = datetime.now(UTC)
    factory = get_session_factory()
    session = factory()
    try:
        uid = new_user(session)
        # Один активный прогон на базу — истёкший lease у running.
        session.add(ParseRunRow(status="running", config_snapshot={}, lease_until=now - timedelta(minutes=10)))
        session.add(LlmJobRow(user_id=uid, kind="import", status="running", lease_until=now - timedelta(minutes=1)))
        session.add(LlmJobRow(user_id=uid, kind="import", status="running", lease_until=now + timedelta(minutes=2)))
        # Не running — не считается, даже без lease.
        session.add(LlmJobRow(user_id=uid, kind="import", status="queued"))
        session.add(HhSyncJobRow(status="completed", params={}))
        session.commit()
    finally:
        session.close()

    assert _sample("db_jobs_lease_expired", {"kind": "parse"}) == 1
    assert _sample("db_jobs_lease_expired", {"kind": "llm"}) == 1
    # Серия с нулём есть: алерт не путает «нет зависших» с «нет метрики».
    assert REGISTRY.get_sample_value("db_jobs_lease_expired", {"kind": "hh_sync"}) == 0


def test_db_jobs_lease_expired_counts_running_without_lease(api_client):
    from backend.db.models import HhSyncJobRow

    factory = get_session_factory()
    session = factory()
    try:
        session.add(HhSyncJobRow(status="running", params={}))
        session.commit()
    finally:
        session.close()

    assert _sample("db_jobs_lease_expired", {"kind": "hh_sync"}) == 1


def test_parse_run_duration_has_fine_buckets():
    from backend.observability import PARSE_RUN_DURATION

    edges = list(PARSE_RUN_DURATION._upper_bounds)
    assert 10.0 in edges
    assert 30.0 in edges
    assert 60.0 in edges


def test_parse_run_started_timestamp_gauge():
    from backend.observability import (
        PARSE_RUN_STARTED_TIMESTAMP,
        mark_parse_run_started,
        mark_parse_run_stopped,
    )

    mark_parse_run_stopped()
    assert PARSE_RUN_STARTED_TIMESTAMP._value.get() == 0.0
    mark_parse_run_started()
    assert PARSE_RUN_STARTED_TIMESTAMP._value.get() > 0.0
    mark_parse_run_stopped()
    assert PARSE_RUN_STARTED_TIMESTAMP._value.get() == 0.0


def test_metrics_not_served_on_api_port(api_client):
    # /metrics — на отдельном порту (API_METRICS_PORT), клиентам API он недоступен.
    assert api_client.get("/metrics").status_code == 404


def test_metrics_server_on_separate_port(api_client, monkeypatch):
    import socket
    import urllib.request

    from backend.main import _start_metrics_server
    from backend.settings import get_settings

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("API_METRICS_PORT", str(port))
    get_settings.cache_clear()
    _register(api_client, "scrape@example.com")
    server = _start_metrics_server()
    assert server is not None
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert "http_requests_total" in body
    assert "auth_events_total" in body
    assert "db_users 1.0" in body


def _empty_db_stats():
    """Снимок ``_DBStatsCollector._read_db`` для пустой БД."""
    return (
        {
            k: 0
            for k in (
                "vacancies_active",
                "vacancies_archived",
                "resumes",
                "users",
                "matches",
                "cover_letters",
                "ai_cache",
            )
        },
        {},
        {},
        0,
        None,
        {"total": 0, "with_detail": 0, "without_detail": 0},
        {},
        {"parse": 0, "llm": 0, "hh_sync": 0},
    )


def test_db_stats_collector_caches_reads(monkeypatch):
    from backend.observability import _DBStatsCollector
    from backend.settings import get_settings

    calls = []

    def _read_db():
        calls.append(1)
        return _empty_db_stats()

    monkeypatch.setattr(_DBStatsCollector, "_read_db", staticmethod(_read_db))
    collector = _DBStatsCollector()

    monkeypatch.setenv("METRICS_DB_STATS_CACHE_SECONDS", "60")
    get_settings.cache_clear()
    list(collector.collect())
    list(collector.collect())
    assert len(calls) == 1

    monkeypatch.setenv("METRICS_DB_STATS_CACHE_SECONDS", "0")
    get_settings.cache_clear()
    list(collector.collect())
    list(collector.collect())
    assert len(calls) == 3
    get_settings.cache_clear()


def test_db_stats_collector_does_not_cache_failures(monkeypatch):
    from backend.observability import _DBStatsCollector
    from backend.settings import get_settings

    def _down():
        raise RuntimeError("postgres down")

    monkeypatch.setattr(_DBStatsCollector, "_read_db", staticmethod(_down))
    monkeypatch.setenv("METRICS_DB_STATS_CACHE_SECONDS", "60")
    get_settings.cache_clear()
    collector = _DBStatsCollector()
    assert list(collector.collect()) == []
    assert collector._cached is None
    get_settings.cache_clear()


def test_db_stats_collector_sets_rq_queue_depths(monkeypatch):
    from backend.observability import _DBStatsCollector

    monkeypatch.setattr(_DBStatsCollector, "_read_db", staticmethod(_empty_db_stats))
    monkeypatch.setattr("backend.queue.rq_conn.llm_queue_length", lambda: 3)
    monkeypatch.setattr("backend.queue.rq_conn.parse_queue_depth", lambda: 1)
    list(_DBStatsCollector().collect())
    assert REGISTRY.get_sample_value("llm_rq_queue_jobs", {"queue": "llm"}) == 3
    assert REGISTRY.get_sample_value("llm_rq_queue_jobs", {"queue": "parse"}) == 1


def test_worker_imports_do_not_register_db_stats_collector():
    """Регрессия: воркеры отдавали db_* (sum(db_users) на дашборде — ×число процессов)."""
    import subprocess
    import sys

    code = (
        "import backend.workers.parse_worker, backend.workers.llm_worker, backend.workers.hh_worker\n"
        "import backend.workers.recover, backend.workers.recover_parse, backend.workers.parse_tasks\n"
        "import backend.workers.llm_tasks\n"
        "from prometheus_client import REGISTRY\n"
        "print(sorted({type(c).__name__ for c in REGISTRY._collector_to_names}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "_DBPoolCollector" in out
    assert "_DBStatsCollector" not in out


def test_register_db_stats_collector_is_idempotent():
    from backend.observability import _DBStatsCollector, register_db_stats_collector

    register_db_stats_collector()
    register_db_stats_collector()
    assert sum(isinstance(c, _DBStatsCollector) for c in REGISTRY._collector_to_names) == 1


def test_multiprocess_metrics_are_summed_and_dead_gauges_collected(tmp_path, monkeypatch):
    """Несколько процессов API: сервер лидера отдаёт сумму, live-gauge'и умерших убираются."""
    import os
    import subprocess
    import sys

    from prometheus_client import generate_latest

    from backend import observability

    child = (
        "from backend.observability import AUTH_EVENTS, LLM_JOB_SSE_CONNECTIONS\n"
        "AUTH_EVENTS.labels(event='login', outcome='success').inc(3)\n"
        "LLM_JOB_SSE_CONNECTIONS.inc()\n"
    )
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for _ in range(2):
        subprocess.run([sys.executable, "-c", child], env=env, check=True, cwd=os.getcwd(), timeout=60)

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    body = generate_latest(observability.metrics_registry()).decode()
    assert 'auth_events_total{event="login",outcome="success"} 6.0' in body
    assert "llm_job_sse_connections 2.0" in body

    # Оба процесса завершились: их live-gauge'и убраны, счётчики остались.
    assert len(observability.collect_dead_process_metrics()) == 2
    body = generate_latest(observability.metrics_registry()).decode()
    assert 'auth_events_total{event="login",outcome="success"} 6.0' in body
    assert "llm_job_sse_connections 2.0" not in body


def test_single_process_metrics_use_global_registry(monkeypatch):
    from prometheus_client import REGISTRY

    from backend import observability

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    assert observability.metrics_registry() is REGISTRY
    assert observability.start_pool_gauges() is None
    assert observability.collect_dead_process_metrics() == []


def test_labelled_counters_have_zero_series_before_first_event():
    """Первое событие после старта процесса видно в increase(): серия уже есть с нулём."""
    for name, labels in (
        ("hh_syncs_total", {"outcome": "success"}),
        ("hh_details_total", {"result": "fetched"}),
        ("auth_events_total", {"event": "login", "outcome": "success"}),
        ("vacancy_match_calls_total", {"outcome": "success"}),
        ("vacancy_import_items_total", {"status": "created"}),
        ("llm_jobs_recover_total", {"from_status": "running"}),
    ):
        assert _has_sample(name, labels), (name, labels)


def test_init_llm_job_series_covers_every_kind():
    from backend import observability
    from backend.services.llm_job_service import ALL_KINDS

    observability.init_llm_job_series(ALL_KINDS, backend="rq")
    for kind in ALL_KINDS:
        assert _has_sample("llm_worker_jobs_total", {"kind": kind, "outcome": "done"})
        assert _has_sample("llm_jobs_total", {"kind": kind, "outcome": "queued"})
        assert _has_sample("llm_job_enqueue_total", {"backend": "rq", "kind": kind, "outcome": "queued"})


def _fake_proc(root, pids):
    """Каталог в формате /proc: btime и stat/limits/fd у каждого pid."""
    root.mkdir()
    (root / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n", encoding="utf-8")
    for pid in pids:
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        for fd in range(pid % 5 + 1):
            (d / "fd" / str(fd)).write_text("", encoding="utf-8")
        # Поля после ")": [11] utime, [12] stime, [19] starttime, [20] vsize, [21] rss (страниц).
        fields = ["S"] + ["0"] * 25
        fields[11], fields[12], fields[19], fields[20], fields[21] = "100", "50", "200", "4096", str(pid)
        (d / "stat").write_text(f"{pid} (python) " + " ".join(fields), encoding="utf-8")
        (d / "limits").write_text(
            "Max open files            1024                 1024                 files\n", encoding="utf-8"
        )


def test_api_processes_collector_labels_every_live_process(tmp_path):
    """Несколько процессов API: process_* каждого живого процесса — со своим pid."""
    from backend.observability import _ApiProcessesCollector

    mp_dir = tmp_path / "mp"
    mp_dir.mkdir()
    for name in ("counter_101.db", "gauge_livesum_102.db", "histogram_103.db", "junk.txt"):
        (mp_dir / name).write_bytes(b"")
    proc = tmp_path / "proc"
    _fake_proc(proc, [101, 102])  # 103 умер: файлы счётчиков остались, /proc/103 нет

    families = {f.name: f for f in _ApiProcessesCollector(str(mp_dir), proc=str(proc)).collect()}
    rss = {s.labels["pid"]: s.value for s in families["process_resident_memory_bytes"].samples}
    assert set(rss) == {"101", "102"}
    assert rss["101"] > 0
    fds = {s.labels["pid"]: s.value for s in families["process_open_fds"].samples}
    assert fds == {"101": 2.0, "102": 3.0}
    assert families["process_cpu_seconds"].type == "counter"


def test_multiprocess_registry_exposes_gc_counters(tmp_path, monkeypatch):
    """GC процессов API попадает в сумму multiprocess-метрик (GCCollector туда не входит)."""
    import os
    import subprocess
    import sys

    from prometheus_client import generate_latest

    from backend import observability

    child = (
        "import gc, time\n"
        "from backend.observability import start_pool_gauges\n"
        "gc.collect()\n"
        "stop = start_pool_gauges()\n"
        "time.sleep(0.5)\n"
        "stop.set()\n"
    )
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    subprocess.run([sys.executable, "-c", child], env=env, check=True, cwd=os.getcwd(), timeout=60)

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    body = generate_latest(observability.metrics_registry()).decode()
    line = next(x for x in body.splitlines() if x.startswith('python_gc_collections_total{generation="2"}'))
    assert float(line.rsplit(" ", 1)[1]) >= 1
