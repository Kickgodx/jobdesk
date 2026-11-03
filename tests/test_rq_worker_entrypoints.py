"""RQ-воркеры целиком: entrypoint ``main()`` → ``run_rq_worker`` → LivenessWorker на fakeredis.

Сервис ставит джобу в очередь (backend ``rq``), воркер забирает её из своей очереди и
выполняет. Так проверяется, что очередь в entrypoint совпадает с очередью enqueue и что
setup процесса (rate gate, recover на старте) не падает. Побочные эффекты процесса —
логирование, .env, порт /metrics, трассировка, watchdog — подменены; ``work()`` идёт в
режиме ``burst`` (выйти, когда очередь пуста), иначе блокировал бы тест.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis
import pytest
from rq import Queue
from tests.conftest import new_user
from tests.llm_job_helpers import use_ai_config

from backend.config_builder import build_config
from backend.db import llm_job_repository as lj
from backend.db import parse_run_repository as pr
from backend.db.models import ResumeRow, VacancyMatchRow, VacancyRow
from backend.db.session import session_scope
from backend.queue import rq_conn
from backend.schemas import RunCreateRequest
from backend.services import llm_job_service, match_service, run_service
from backend.services.llm_job_service import KIND_MATCH, llm_job_runner
from backend.services.llm_rate_gate import RedisWindowRateGate
from backend.settings import get_settings
from backend.workers import liveness, llm_worker, parse_tasks, parse_worker, runner
from jobdesk.ai import cache_registry, rate_limit
from jobdesk.ai.clients.openrouter import LLMUsage, OpenRouterClient
from jobdesk.ai.prompts import MATCH_PROMPT_VERSION
from jobdesk.ai.schema import VacancyAIExtraction, VacancyMatchExtraction
from jobdesk.models.metrics import MetricsSummary
from jobdesk.models.parse_result import ParseResult

_executed: list[str] = []


def record_job(value: str) -> str:
    """Джоба для тестов ``run_rq_worker``: RQ импортирует её по dotted path."""
    _executed.append(value)
    return value


@pytest.fixture
def fake_redis(pg_db, monkeypatch):
    """Джобы ядра (прогон, LLM) — через RQ на общем fakeredis; HH-воркер — tests/test_hh_worker.py."""
    server = fakeredis.FakeStrictRedis()
    for env in ("PARSE_JOB_BACKEND", "LLM_JOB_BACKEND"):
        monkeypatch.setenv(env, "rq")
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    get_settings.cache_clear()
    rq_conn.reset_redis_connection()
    monkeypatch.setattr(rq_conn, "get_redis_connection", lambda force_new=False: server)
    # _setup воркеров регистрирует глобальный rate gate — вернуть прежний после теста.
    monkeypatch.setattr(rate_limit, "_global_gate", rate_limit._global_gate)
    # conftest регистрирует Postgres-кэш LLM на всю сессию — это скрывало воркер,
    # который его не регистрирует. Здесь адаптер обязан подключить сам _setup воркера.
    monkeypatch.setattr(cache_registry, "_postgres_factory", None)
    _executed.clear()
    yield server
    rq_conn.reset_redis_connection()
    get_settings.cache_clear()


@pytest.fixture
def worker_process(monkeypatch):
    """Подменить побочные эффекты процесса воркера; вернуть запись того, что он сделал."""
    rec = SimpleNamespace(
        pool_role=None,
        metrics_ports=[],
        tracing_shutdowns=0,
        recover_stop=None,
        recover_interval=None,
        watchdog_stop=None,
        watchdog_seconds=None,
        queues=None,
        worker_name=None,
    )

    def shutdown_tracing() -> None:
        rec.tracing_shutdowns += 1

    def start_periodic_recover(fn, *, interval_seconds, name):
        rec.recover_interval = interval_seconds
        rec.recover_stop = threading.Event()
        return rec.recover_stop

    def start_watchdog(stall_seconds, *, name):
        rec.watchdog_seconds = stall_seconds
        rec.watchdog_stop = None if stall_seconds <= 0 else threading.Event()
        return rec.watchdog_stop

    original_work = liveness.LivenessWorker.work

    def burst_work(self, *args, **kwargs):
        rec.queues = self.queue_names()
        rec.worker_name = self.name
        return original_work(self, burst=True, with_scheduler=False)

    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner, "try_load_dotenv", lambda: None)
    monkeypatch.setattr(runner, "setup_tracing", lambda app, settings: False)
    monkeypatch.setattr(runner, "shutdown_tracing", shutdown_tracing)
    monkeypatch.setattr("backend.db.session.set_pool_role", lambda role: setattr(rec, "pool_role", role))
    monkeypatch.setattr("prometheus_client.start_http_server", lambda port: rec.metrics_ports.append(port))
    monkeypatch.setattr("backend.workers.recover_loop.start_periodic_recover", start_periodic_recover)
    monkeypatch.setattr(liveness, "start_watchdog", start_watchdog)
    monkeypatch.setattr(liveness.LivenessWorker, "work", burst_work)
    # runner делает setdefault; setenv + delenv — чтобы monkeypatch убрал значение после теста.
    monkeypatch.setenv("OTEL_SERVICE_NAME", "x")
    monkeypatch.delenv("OTEL_SERVICE_NAME")
    return rec


def _assert_process_lifecycle(rec, *, role: str, queue: str, metrics_port: int) -> None:
    assert rec.pool_role == role
    assert rec.queues == [queue]
    assert rec.metrics_ports == [metrics_port]
    assert os.environ["OTEL_SERVICE_NAME"] == f"jobdesk-{role}"
    assert rec.recover_interval == get_settings().job_recover_interval_seconds
    assert rec.watchdog_seconds == get_settings().worker_stall_exit_seconds
    # После выхода из work() фоновые потоки остановлены, трассировка сброшена.
    assert rec.recover_stop.is_set()
    assert rec.watchdog_stop.is_set()
    assert rec.tracing_shutdowns == 1


# --- entrypoint'ы: enqueue сервисом → main() воркера выполняет ---


def test_parse_worker_runs_queued_parse_run(fake_redis, worker_process, monkeypatch):
    calls: list = []

    def fake_run_parse(config, **kwargs):
        calls.append(config)
        return ParseResult(
            run_params={},
            posts_fetched=0,
            vacancies_all=[],
            vacancies_unique=[],
            parse_warnings="",
            metrics=MetricsSummary(),
        )

    monkeypatch.setattr(parse_tasks, "run_parse", fake_run_parse)
    request = RunCreateRequest()
    run_id = run_service.start_parse_run(build_config(get_settings().config_path, request), request)
    assert Queue(rq_conn.PARSE_QUEUE_NAME, connection=fake_redis).count == 1

    parse_worker.main()

    with session_scope() as session:
        row = pr.get_parse_run(session, run_id)
        assert row.status == "completed", row.error_message
        assert row.attempts == 1
    assert len(calls) == 1
    assert Queue(rq_conn.PARSE_QUEUE_NAME, connection=fake_redis).count == 0
    # _setup: парсинг ходит в LLM — общий лимит запросов через Redis и кэш LLM в Postgres.
    assert isinstance(rate_limit._global_gate, RedisWindowRateGate)
    assert cache_registry.get_postgres_cache_factory() is not None
    _assert_process_lifecycle(
        worker_process,
        role="parse-worker",
        queue=rq_conn.PARSE_QUEUE_NAME,
        metrics_port=get_settings().parse_worker_metrics_port,
    )


def test_llm_worker_runs_queued_match_job(fake_redis, worker_process, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(match_service, "match_single", lambda **kwargs: calls.append(kwargs))
    with session_scope() as session:
        user_id = new_user(session)
        resume = ResumeRow(user_id=user_id, name="CV", text="Python, 5 лет")
        session.add(resume)
        session.add(
            VacancyRow(
                id="vac-worker",
                source_post_key="ch-worker",
                source_channel="ch",
                source_url="https://t.me/s/ch",
                post_date=datetime(2026, 9, 1, tzinfo=UTC),
                raw_text="Python developer",
                cleaned_text="Python developer",
            )
        )
        session.flush()
        resume_id = resume.id

    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_MATCH, resume_id=resume_id, vacancy_id="vac-worker"
    ).job_id
    assert Queue(rq_conn.LLM_QUEUE_NAME, connection=fake_redis).count == 1

    llm_worker.main()

    with session_scope() as session:
        row = lj.get_llm_job(session, job_id)
        assert row.status == "done", row.error_message
    assert [(c["resume_id"], c["vacancy_id"]) for c in calls] == [(resume_id, "vac-worker")]
    assert Queue(rq_conn.LLM_QUEUE_NAME, connection=fake_redis).count == 0
    # Реплики llm-worker делят один лимит запросов, а не умножают его.
    assert isinstance(rate_limit._global_gate, RedisWindowRateGate)
    assert cache_registry.get_postgres_cache_factory() is not None
    _assert_process_lifecycle(
        worker_process,
        role="llm-worker",
        queue=rq_conn.LLM_QUEUE_NAME,
        metrics_port=get_settings().llm_worker_metrics_port,
    )


def test_llm_worker_match_job_records_requested_model(fake_redis, worker_process, monkeypatch, tmp_path):
    """Матч через RQ-воркер пишет запрошенную модель и версию промпта (основа ``stale``)."""
    use_ai_config(monkeypatch, tmp_path, match_model="match/a")

    class _FakeClient:
        def match_with_fallback(self, *, resume_text, vacancy_text):
            return VacancyMatchExtraction(confidence=80, explanation="ok"), LLMUsage(model="fallback/b")

    monkeypatch.setattr(match_service, "get_interactive_client", lambda: _FakeClient())
    with session_scope() as session:
        user_id = new_user(session)
        resume = ResumeRow(user_id=user_id, name="CV", text="Python")
        session.add(resume)
        session.add(
            VacancyRow(
                id="vac-stale",
                source_post_key="ch-stale",
                source_channel="ch",
                source_url="https://t.me/s/ch",
                raw_text="Python developer",
                cleaned_text="Python developer",
            )
        )
        session.flush()
        resume_id = resume.id

    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_MATCH, resume_id=resume_id, vacancy_id="vac-stale"
    ).job_id
    llm_worker.main()

    with session_scope() as session:
        assert lj.get_llm_job(session, job_id).status == "done"
        row = session.get(VacancyMatchRow, (resume_id, "vac-stale"))
        assert (row.model, row.requested_model, row.prompt_version) == ("fallback/b", "match/a", MATCH_PROMPT_VERSION)


def _resume_and_vacancy(vacancy_id: str) -> tuple[int, int]:
    with session_scope() as session:
        user_id = new_user(session)
        resume = ResumeRow(user_id=user_id, name="CV", text="Python")
        session.add(resume)
        session.add(
            VacancyRow(
                id=vacancy_id,
                source_post_key=f"ch-{vacancy_id}",
                source_channel="ch",
                source_url="https://t.me/s/ch",
                raw_text="Python developer",
                cleaned_text="Python developer",
            )
        )
        session.flush()
        return user_id, resume.id


def test_llm_worker_skips_job_cancelled_in_queue(fake_redis, worker_process, monkeypatch):
    """Отменённая в очереди задача: RQ-джоба снята или воркер её не берёт — LLM не вызывается."""
    calls: list[dict] = []
    monkeypatch.setattr(match_service, "match_single", lambda **kwargs: calls.append(kwargs))
    user_id, resume_id = _resume_and_vacancy("vac-cancel")
    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_MATCH, resume_id=resume_id, vacancy_id="vac-cancel"
    ).job_id

    assert llm_job_service.cancel_llm_job(job_id, user_id=user_id) is True
    llm_worker.main()

    with session_scope() as session:
        assert lj.get_llm_job(session, job_id).status == "cancelled"
    assert calls == []


def test_llm_worker_stops_import_with_cancel_requested(fake_redis, worker_process, monkeypatch):
    """Флаг отмены у выполняемого импорта: llm-worker останавливается перед следующим текстом.

    AI в тестовом конфиге выключен — тексты разбираются правилами, точка проверки та же.
    """
    from backend.services import import_service
    from backend.services.llm_job_service import KIND_IMPORT

    with session_scope() as session:
        user_id = new_user(session)
    texts = [f"Вакансия: Python-разработчик #{i}. Удалёнка, от 200 000 руб." for i in range(3)]
    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_IMPORT, params=import_service.job_params(texts, None)
    ).job_id
    real_commit_vacancy = import_service.vacancy_repo.upsert_vacancies

    def upsert_then_cancel(session, vacancies, **kwargs):
        out = real_commit_vacancy(session, vacancies, **kwargs)
        lj.request_llm_job_cancel(session, job_id)  # отмена пришла во время первого текста
        return out

    monkeypatch.setattr(import_service.vacancy_repo, "upsert_vacancies", upsert_then_cancel)
    llm_worker.main()

    with session_scope() as session:
        row = lj.get_llm_job(session, job_id)
        assert (row.status, row.cancel_requested) == ("cancelled", True)
        assert [it["status"] for it in row.result["items"]] == ["created"]


def test_llm_worker_runs_import_with_llm(fake_redis, worker_process, monkeypatch, default_test_config):
    """Импорт с LLM в llm-worker идёт через настоящий AIEnrichmentService (Postgres-кэш).

    Регрессия: воркер не регистрировал Postgres-кэш LLM, сервис не создавался, и импорт
    молча разбирал тексты правилами — без проверки «это вакансия?».
    """
    from backend.db.models import AIExtractionCacheRow
    from backend.services import import_service
    from backend.services.llm_job_service import KIND_IMPORT
    from jobdesk.config.loader import load_config

    cfg = load_config(default_test_config)
    ai_cfg = cfg.model_copy(
        update={"ai": cfg.ai.model_copy(update={"enabled": True, "use_cache": True, "cache_backend": "postgres"})}
    )
    monkeypatch.setattr(import_service, "load_config", lambda *a, **k: ai_cfg)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    llm_calls: list[str] = []

    def fake_extract(self, *, text, search_context):
        llm_calls.append(text)
        return VacancyAIExtraction(is_job_post=True, confidence=0.9), LLMUsage(model="test/model")

    monkeypatch.setattr(OpenRouterClient, "extract_with_fallback", fake_extract)
    text = (
        "Вакансия: Senior Python-разработчик в компанию Acme.\n"
        "Зарплата: от 250 000 до 350 000 руб. на руки.\n"
        "Контакт: @recruiter_acme"
    )
    with session_scope() as session:
        user_id = new_user(session)
    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_IMPORT, params=import_service.job_params([text], None)
    ).job_id

    llm_worker.main()

    with session_scope() as session:
        row = lj.get_llm_job(session, job_id)
        assert row.status == "done", row.error_message
        assert row.result["ai_enabled"] is True, row.result
        assert row.result["warning"] is None, row.result
        (item,) = row.result["items"]
        assert item["status"] == "created"
        vacancy = session.get(VacancyRow, item["vacancy_id"])
        assert vacancy.extraction_source != "rules"
        # Ответ модели лёг в Postgres-кэш — адаптер подключён в процессе воркера.
        assert session.query(AIExtractionCacheRow).count() == 1
    assert len(llm_calls) == 1


def test_llm_worker_fails_import_when_llm_does_not_start(fake_redis, worker_process, monkeypatch, default_test_config):
    """RQ-путь: LLM в воркере не запустился — импорт-задача error, текст не сохранён правилами."""
    from backend.services import import_service
    from backend.services.llm_job_service import KIND_IMPORT
    from jobdesk.config.loader import load_config

    cfg = load_config(default_test_config)
    ai_cfg = cfg.model_copy(update={"ai": cfg.ai.model_copy(update={"enabled": True})})
    monkeypatch.setattr(import_service, "load_config", lambda *a, **k: ai_cfg)

    def broken(*args, **kwargs):
        raise ValueError("OPENROUTER_API_KEY не задан")

    monkeypatch.setattr("jobdesk.ai.enricher.AIEnrichmentService", broken)
    with session_scope() as session:
        user_id = new_user(session)
    job_id = llm_job_runner.enqueue(
        user_id=user_id, kind=KIND_IMPORT, params=import_service.job_params(["Вакансия: Python-разработчик"], None)
    ).job_id

    llm_worker.main()

    with session_scope() as session:
        row = lj.get_llm_job(session, job_id)
        assert row.status == "error"
        assert "OPENROUTER_API_KEY не задан" in row.error_message
        assert session.query(VacancyRow).count() == 0


# --- run_rq_worker: отказоустойчивость старта и очистка ---


def _run(*, recover_fn=lambda: 0, job_backend: str = "rq", setup=None) -> None:
    runner.run_rq_worker(
        role="test-worker",
        queue_name="test",
        metrics_port=0,
        recover_fn=recover_fn,
        job_backend=job_backend,
        job_backend_env="TEST_JOB_BACKEND",
        setup=setup,
    )


def test_run_rq_worker_requires_redis_url(fake_redis, worker_process, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    recovered: list[int] = []

    with pytest.raises(SystemExit) as exc:
        _run(recover_fn=lambda: recovered.append(1) or 0)

    assert exc.value.code == 1
    assert recovered == []  # до recover и до work() не дошли
    assert worker_process.queues is None


def test_run_rq_worker_survives_startup_failures(fake_redis, worker_process, monkeypatch, caplog):
    """Ни OTel, ни порт /metrics, ни recover на старте не мешают воркеру взять джобу."""

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "setup_tracing", broken)
    monkeypatch.setattr("prometheus_client.start_http_server", broken)
    setup_calls: list[int] = []
    Queue("test", connection=fake_redis).enqueue(record_job, "done")

    with caplog.at_level("WARNING"):
        _run(recover_fn=broken, job_backend="inprocess", setup=lambda: setup_calls.append(1))

    assert _executed == ["done"]
    assert setup_calls == [1]
    messages = caplog.text
    assert "OTel для test-worker не включён" in messages
    assert "Не удалось стартовать metrics HTTP" in messages
    assert "Recover on start failed" in messages
    assert "TEST_JOB_BACKEND=inprocess" in messages
    assert worker_process.recover_stop.is_set()
    assert worker_process.tracing_shutdowns == 1


def test_run_rq_worker_reports_recovered_jobs(fake_redis, worker_process, caplog):
    with caplog.at_level("INFO"):
        _run(recover_fn=lambda: 3)

    assert "Recover on start: 3 job(s)" in caplog.text


def test_run_rq_worker_cleans_up_when_worker_crashes(fake_redis, worker_process, monkeypatch):
    def crash(self, *args, **kwargs):
        raise RuntimeError("redis connection lost")

    monkeypatch.setattr(liveness.LivenessWorker, "work", crash)

    with pytest.raises(RuntimeError, match="redis connection lost"):
        _run()

    assert worker_process.recover_stop.is_set()
    assert worker_process.watchdog_stop.is_set()
    assert worker_process.tracing_shutdowns == 1


def test_run_rq_worker_without_watchdog(fake_redis, worker_process, monkeypatch):
    """WORKER_STALL_EXIT_SECONDS=0 — watchdog выключен, finally это переживает."""
    monkeypatch.setenv("WORKER_STALL_EXIT_SECONDS", "0")
    get_settings.cache_clear()

    _run()

    assert worker_process.watchdog_seconds == 0
    assert worker_process.watchdog_stop is None
    assert worker_process.tracing_shutdowns == 1


# --- имя воркера ---


def test_worker_name_is_unique_per_call():
    a, b = runner.worker_name("parse-worker"), runner.worker_name("parse-worker")

    assert a != b
    assert f"-{os.getpid()}-" in a


def test_worker_name_falls_back_to_role_without_hostname(monkeypatch):
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "  ")

    assert runner.worker_name("hh-worker").startswith(f"hh-worker-{os.getpid()}-")
