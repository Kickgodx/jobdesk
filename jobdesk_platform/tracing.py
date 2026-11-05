"""OpenTelemetry-трассировка процессов JobDesk: SDK, OTLP-экспорт и автоинструментации.

Включение и параметры передаёт процесс (у ядра — ``OTEL_ENABLED=true`` в ``backend.tracing``); доля сохраняемых трасс —
``OTEL_TRACES_SAMPLE_RATIO`` (head sampling, 1.0 = все). Экспорт — стандартные
переменные SDK: ``OTEL_EXPORTER_OTLP_ENDPOINT`` (по умолчанию
``http://localhost:4318``), ``OTEL_SERVICE_NAME``.

Sampler отбрасывает CLIENT-span'ы без родителя: SQL из ``/metrics``-коллектора
(каждый scrape) и фоновых потоков иначе стали бы тысячами трасс из одного span'а.

Автоматически: входящие HTTP (FastAPI), SQL (SQLAlchemy), исходящие HTTP
(httpx — OpenRouter через openai SDK, ЦБ). Доменные span'ы (прогон парсинга,
LLM, матчинг, письма) — через ``opentelemetry.trace`` в коде; без SDK это
no-op, поэтому CLI и тесты работают без коллектора.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import get_host_port_url_tuple
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind

_LOG = logging.getLogger(__name__)

_provider: TracerProvider | None = None

QueryRedactor = Callable[[str], str]


def service_name(default: str) -> str:
    """``OTEL_SERVICE_NAME`` процесса (воркеры задают свой), иначе ``default``.

    Явный атрибут в ``Resource.create`` перекрывает env-детектор SDK — без этой
    функции все воркеры попадали в Tempo как ``jobdesk-backend``.
    """
    return os.environ.get("OTEL_SERVICE_NAME", "").strip() or default


class SkipOrphanClientSpans(Sampler):
    """Root-sampler: CLIENT-span без родителя (SQL/HTTP вне запроса и доменного span'а) — DROP."""

    def __init__(self, delegate: Sampler) -> None:
        self._delegate = delegate

    def should_sample(
        self,
        parent_context,
        trace_id,
        name,
        kind=None,
        attributes=None,
        links=None,
        trace_state=None,
    ) -> SamplingResult:
        if kind == SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return self._delegate.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return f"SkipOrphanClientSpans{{{self._delegate.get_description()}}}"


def build_sampler(ratio: float) -> Sampler:
    return ParentBased(root=SkipOrphanClientSpans(TraceIdRatioBased(min(max(ratio, 0.0), 1.0))))


def excluded_urls(paths: frozenset[str]) -> str:
    """Пути → список regex для ``excluded_urls`` (ищется по ``scheme://host/path``).

    Якорим на начало пути и конец строки: голый ``/metrics`` совпал бы и с
    ``/api/v1/vacancies/metrics``.
    """
    return ",".join(f"://[^/]+{re.escape(p)}$" for p in sorted(paths))


def _request_hook(redact_query: QueryRedactor):
    """Хук FastAPI-инструментации: маскировать секреты в query (``?ticket=`` у SSE)."""

    def hook(span, scope) -> None:
        _redact_span_query(span, scope, redact_query)

    return hook


def _redact_span_query(span, scope, redact_query: QueryRedactor) -> None:
    if span is None or not span.is_recording():
        return
    query = (scope.get("query_string") or b"").decode("latin-1")
    if not query:
        return
    safe = redact_query(query)
    attrs = getattr(span, "attributes", None) or {}
    if "http.url" in attrs:
        _, _, url = get_host_port_url_tuple(scope)
        span.set_attribute("http.url", f"{url}?{safe}")
    if "url.query" in attrs:
        span.set_attribute("url.query", safe)


def instrument_app(
    app: FastAPI,
    *,
    exclude_paths: frozenset[str],
    tracer_provider=None,
    redact_query: QueryRedactor | None = None,
) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls=excluded_urls(exclude_paths),
        exclude_spans=["receive", "send"],
        server_request_hook=_request_hook(redact_query) if redact_query is not None else None,
    )


def setup_tracing(
    app: FastAPI,
    *,
    enabled: bool,
    sample_ratio: float,
    exclude_paths: frozenset[str],
    default_service_name: str,
    redact_query: QueryRedactor | None = None,
) -> bool:
    """Глобальный TracerProvider + инструментации; ``False``, если выключено."""
    global _provider
    if not enabled:
        return False
    if _provider is not None:
        return True
    resource = Resource.create({SERVICE_NAME: service_name(default_service_name), SERVICE_VERSION: app.version})
    provider = TracerProvider(resource=resource, sampler=build_sampler(sample_ratio))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    instrument_app(app, exclude_paths=exclude_paths, redact_query=redact_query)
    SQLAlchemyInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    _provider = provider
    _LOG.info("OpenTelemetry: трассировка включена (%s).", resource.attributes[SERVICE_NAME])
    return True


def current_traceparent() -> str | None:
    """W3C ``traceparent`` текущего span'а — передаётся в джобу, чтобы воркер продолжил трассу."""
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get("traceparent")
    except Exception:
        return None


def shutdown_tracing() -> None:
    """Дослать буфер BatchSpanProcessor при остановке процесса."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
