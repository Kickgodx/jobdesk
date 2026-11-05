"""OpenTelemetry: sampler, excluded_urls, FastAPI-span'ы, корреляция с логами, LLM/parse span'ы."""

from __future__ import annotations

import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from backend.http_logging import HttpLoggingMiddleware
from backend.tracing import excluded_urls, instrument_app
from jobdesk.utils.logger import RequestIdFilter


@pytest.fixture
def exporter(otel_exporter):
    return otel_exporter


@pytest.fixture
def log_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s | %(context_part)s%(message)s"))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_excluded_urls_are_anchored_to_full_path():
    patterns = [re.compile(p) for p in excluded_urls(frozenset({"/metrics", "/api/v1/health"})).split(",")]

    def disabled(url: str) -> bool:
        return any(p.search(url) for p in patterns)

    assert disabled("http://backend:8000/metrics")
    assert disabled("http://localhost/api/v1/health")
    assert not disabled("http://localhost/api/v1/vacancies/metrics")
    assert not disabled("http://localhost/api/v1/health/extra")


def test_sampler_drops_only_orphan_client_spans(exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("orphan sql", kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("parse_run"), tracer.start_as_current_span("child sql", kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER):
        pass
    assert sorted(s.name for s in exporter.get_finished_spans()) == [
        "GET /x",
        "child sql",
        "parse_run",
    ]


@pytest.fixture
def traced_client(exporter):
    app = FastAPI()
    log = logging.getLogger("backend.test_route")

    @app.get("/items")
    def items():
        log.info("inside route")
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        return {"ok": True}

    app.add_middleware(HttpLoggingMiddleware)
    instrument_app(
        app,
        exclude_paths=frozenset({"/metrics"}),
        tracer_provider=trace.get_tracer_provider(),
    )
    with TestClient(app) as c:
        yield c


def test_server_span_has_request_id_and_masked_query(traced_client, exporter, log_stream):
    r = traced_client.get("/items?access_token=supersecret&page=2")
    rid = r.headers["x-request-id"]

    server = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert len(server) == 1
    span = server[0]
    assert span.attributes["request_id"] == rid
    assert not any("supersecret" in str(v) for v in span.attributes.values())
    assert [s for s in exporter.get_finished_spans() if s.name.endswith((" send", " receive"))] == []

    trace_id = format(span.context.trace_id, "032x")
    lines = log_stream.getvalue().splitlines()
    assert f"backend.test_route | request_id={rid} trace_id={trace_id} | inside route" in lines
    assert any(
        line.startswith(
            f"backend.http | request_id={rid} trace_id={trace_id} | RESPONSE GET /items?access_token=***&page=2 200 "
        )
        for line in lines
    )
    assert not any("supersecret" in line for line in lines if line.startswith("backend."))


def test_excluded_path_has_no_span(traced_client, exporter):
    traced_client.get("/metrics")
    assert exporter.get_finished_spans() == ()


def test_llm_call_span_has_genai_attributes(exporter, monkeypatch):
    from jobdesk.ai.clients.openrouter import OpenRouterClient
    from jobdesk.config.schema import AiConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = OpenRouterClient(AiConfig(model="parse/model", cover_letter_model="writer/model"))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Письмо"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )
    monkeypatch.setattr(client, "_create_completion", lambda kwargs: response)

    with trace.get_tracer("test").start_as_current_span("cover_letter.generate"):
        text, _ = client.cover_letter(resume_text="python", vacancy_text="vacancy")
    assert text == "Письмо"

    span = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat "))
    assert span.name == "chat writer/model"
    assert span.attributes["gen_ai.request.model"] == "writer/model"
    assert span.attributes["llm.task"] == "cover_letter"
    assert span.attributes["llm.outcome"] == "success"
    assert span.attributes["gen_ai.usage.input_tokens"] == 120
    assert span.attributes["gen_ai.usage.output_tokens"] == 30
    assert "Письмо" not in str(dict(span.attributes))


@pytest.mark.parametrize(
    "instrumentor_path",
    [
        "opentelemetry.instrumentation.fastapi:FastAPIInstrumentor",
        "opentelemetry.instrumentation.sqlalchemy:SQLAlchemyInstrumentor",
        "opentelemetry.instrumentation.httpx:HTTPXClientInstrumentor",
    ],
)
def test_installed_libraries_satisfy_instrumentations(instrumentor_path):
    """При конфликте версий instrument() молча ничего не делает (так SQL пропал из трасс в Docker)."""
    import importlib

    from opentelemetry.instrumentation.dependencies import get_dependency_conflicts

    module, cls = instrumentor_path.split(":")
    instrumentor = getattr(importlib.import_module(module), cls)()
    assert get_dependency_conflicts(instrumentor.instrumentation_dependencies()) is None


def test_sql_spans_are_children_of_current_span(exporter, pg_db):
    import sqlalchemy
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    instrumentor.instrument(tracer_provider=trace.get_tracer_provider())
    try:
        engine = sqlalchemy.create_engine(pg_db)
        with (
            trace.get_tracer("test").start_as_current_span("vacancy.match") as parent,
            engine.connect() as conn,
        ):
            conn.execute(sqlalchemy.text("SELECT 1"))
        engine.dispose()
    finally:
        instrumentor.uninstrument()

    sql = [s for s in exporter.get_finished_spans() if s.name.startswith("SELECT")]
    assert sql and all(s.parent.span_id == parent.get_span_context().span_id for s in sql)


def test_main_serves_the_instrumented_app_object(monkeypatch):
    """Строка "backend.main:app" при `python -m backend.main` дала бы второй, неинструментированный app."""
    import uvicorn

    import backend.main as backend_main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: calls.append(target))
    backend_main.main()
    assert calls == [backend_main.app]


def test_post_workers_inherit_parent_span(exporter):
    from jobdesk.parse_runner import _submit_in_context

    seen: list[int] = []

    def process_one(post):
        seen.append(trace.get_current_span().get_span_context().trace_id)
        return post

    with (
        trace.get_tracer("test").start_as_current_span("parse.process_posts") as parent,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = [f.result() for f in [_submit_in_context(pool, process_one, p) for p in [1, 2, 3, 4]]]
    assert results == [1, 2, 3, 4]
    assert seen == [parent.get_span_context().trace_id] * 4


def test_trace_crosses_core_hh_service_boundary(exporter, hh_client, monkeypatch):
    """Вызов ядра → HH-сервис → фоновая джоба sync: одна трасса (platform/internal-services).

    Запрос к сервису идёт в пустом ``contextvars``-контексте: как у отдельного процесса, связь
    с трассой ядра — только заголовок ``traceparent``, который ставит клиент.
    """
    import contextvars
    import time as _time

    import httpx
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from tests.conftest import clear_settings_caches

    from backend.services import hh_service_client
    from jobdesk.contracts.hh_service import SYNC_PATH, HhSyncAccepted, HhSyncJobOut, sync_job_path
    from jobdesk_hh import tasks
    from jobdesk_hh.api import app as hh_app

    monkeypatch.setenv("HH_API_TOKEN", "t")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "a@b.c")
    clear_settings_caches()
    monkeypatch.setattr(tasks, "run_hh_sync", lambda session, **kw: tasks.HhSyncResult())

    class FreshContext(httpx.BaseTransport):
        def __init__(self, inner: httpx.BaseTransport) -> None:
            self._inner = inner

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return contextvars.Context().run(self._inner.handle_request, request)

    hh_service_client.set_client_overrides(
        httpx.Client(base_url="http://testserver", transport=FreshContext(TestClient(hh_app)._transport))
    )
    FastAPIInstrumentor.instrument_app(hh_app, tracer_provider=trace.get_tracer_provider())
    hh_app.middleware_stack = hh_app.build_middleware_stack()
    try:
        with trace.get_tracer("test").start_as_current_span("core.request") as root:
            accepted = hh_service_client.call(
                "POST", SYNC_PATH, model=HhSyncAccepted, json={}, user_id=1, idempotent=False
            )
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline:
            job = hh_service_client.call("GET", sync_job_path(accepted.job_id), model=HhSyncJobOut, idempotent=True)
            if job.status == "completed":
                break
            _time.sleep(0.05)
        assert job.status == "completed"
    finally:
        FastAPIInstrumentor.uninstrument_app(hh_app)

    trace_id = root.get_span_context().trace_id
    spans = exporter.get_finished_spans()
    service_spans = [s for s in spans if s.kind == SpanKind.SERVER and s.context.trace_id == trace_id]
    assert [s.name for s in service_spans if s.name == f"POST {SYNC_PATH}"], [s.name for s in spans]
    (job_span,) = [s for s in spans if s.name == "hh_sync_job"]
    assert job_span.context.trace_id == trace_id
