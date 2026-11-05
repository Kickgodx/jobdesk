"""Трассировка ядра: настройки ``backend.settings`` поверх ``jobdesk_platform.tracing``.

Включается ``OTEL_ENABLED=true``; доля сохраняемых трасс — ``OTEL_TRACES_SAMPLE_RATIO``.
Секреты в query (``?ticket=`` у SSE) маскируются в атрибутах span'а тем же правилом, что
и в HTTP-журнале (``backend.http_logging.redact_query``).
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.http_logging import redact_query
from backend.settings import Settings
from jobdesk_platform import tracing as _impl
from jobdesk_platform.tracing import (  # noqa: F401 — реэкспорт для старых импортов
    SkipOrphanClientSpans,
    build_sampler,
    current_traceparent,
    excluded_urls,
    shutdown_tracing,
)

DEFAULT_SERVICE_NAME = "jobdesk-backend"


def service_name() -> str:
    """``OTEL_SERVICE_NAME`` процесса (воркеры задают свой), иначе имя backend."""
    return _impl.service_name(DEFAULT_SERVICE_NAME)


def instrument_app(app: FastAPI, *, exclude_paths: frozenset[str], tracer_provider=None) -> None:
    _impl.instrument_app(app, exclude_paths=exclude_paths, tracer_provider=tracer_provider, redact_query=redact_query)


def setup_tracing(app: FastAPI, settings: Settings) -> bool:
    """Глобальный TracerProvider + инструментации; ``False``, если выключено."""
    return _impl.setup_tracing(
        app,
        enabled=settings.otel_enabled,
        sample_ratio=settings.otel_traces_sample_ratio,
        exclude_paths=settings.http_log_exclude_path_set,
        default_service_name=DEFAULT_SERVICE_NAME,
        redact_query=redact_query,
    )
