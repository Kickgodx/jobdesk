"""Клиент ядра к HH-сервису (``jobdesk_hh``): публичные ``/api/v1/hh/*`` проксируются сюда.

Ошибка сервиса в едином формате отдаётся клиенту как есть (статус и ``detail``); сеть,
тайм-аут, ответ не по контракту или пустой ``HH_SERVICE_URL`` — 503 ``UPSTREAM_UNAVAILABLE``
с ``retryable: true``. Тесты подставляют клиенты к приложению сервиса в том же процессе.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from backend.settings import get_settings
from jobdesk.contracts.hh_service import USER_ID_HEADER
from jobdesk_platform.errors import CODE_UPSTREAM_UNAVAILABLE, AppError
from jobdesk_platform.service_client import ServiceClient, UpstreamError, UpstreamUnavailable

_LOG = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = "HH-сервис недоступен, повторите позже"

_lock = threading.Lock()
_http_override: httpx.Client | None = None
_async_factory_override: Callable[[], httpx.AsyncClient] | None = None


def set_client_overrides(
    http: httpx.Client | None,
    async_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> None:
    """Тесты: ходить в HH-сервис через готовые клиенты (``TestClient`` / ASGI-транспорт)."""
    global _http_override, _async_factory_override
    with _lock:
        _http_override = http
        _async_factory_override = async_factory


def unavailable(reason: str = "") -> AppError:
    if reason:
        _LOG.warning("HH-сервис недоступен: %s", reason)
    return AppError(CODE_UPSTREAM_UNAVAILABLE, UNAVAILABLE_MESSAGE, status_code=503, retryable=True)


def from_upstream_error(e: UpstreamError) -> AppError:
    detail = e.detail
    return AppError(
        detail["code"],
        detail["message"],
        status_code=e.status_code,
        retryable=bool(detail.get("retryable", False)),
    )


def _base_url() -> str:
    return (get_settings().hh_service_url or "").strip()


def _client() -> ServiceClient:
    settings = get_settings()
    if _http_override is None and not _base_url():
        raise UpstreamUnavailable("HH_SERVICE_URL не задан")
    return ServiceClient(
        _base_url() or "http://hh-service",
        settings.hh_service_token or "",
        service="hh",
        timeout=settings.hh_service_timeout_seconds,
        client=_http_override,
    )


def _headers(user_id: int | None) -> dict[str, str]:
    return {USER_ID_HEADER: str(user_id)} if user_id is not None else {}


def call[ModelT: BaseModel](
    method: str,
    path: str,
    *,
    model: type[ModelT] | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
    user_id: int | None = None,
    idempotent: bool,
) -> Any:
    """Вызвать сервис; ответ проверяется ``model`` (если сервис вернул список — каждый элемент)."""
    try:
        with _client() as client:
            data = client.request_json(
                method, path, json=json, params=params, headers=_headers(user_id), idempotent=idempotent
            )
    except UpstreamError as e:
        raise from_upstream_error(e) from e
    except UpstreamUnavailable as e:
        raise unavailable(str(e)) from e
    if model is None:
        return data
    try:
        if isinstance(data, list):
            return [model.model_validate(item) for item in data]
        return model.model_validate(data)
    except ValidationError as e:
        raise unavailable(f"ответ {method} {path} не по контракту: {e}") from e


def async_client() -> httpx.AsyncClient:
    """Async-клиент для потока событий; вызывающий закрывает его сам."""
    settings = get_settings()
    if _async_factory_override is not None:
        client = _async_factory_override()
    else:
        if not _base_url():
            raise unavailable("HH_SERVICE_URL не задан")
        client = httpx.AsyncClient(
            base_url=_base_url(),
            # Поток событий живёт долго: ограничено только соединение и ожидание байтов.
            timeout=httpx.Timeout(settings.hh_service_timeout_seconds, read=None),
        )
    client.headers["Authorization"] = f"Bearer {settings.hh_service_token or ''}"
    return client


def stream_headers(user_id: int | None) -> dict[str, str]:
    """Заголовки потока событий: id пользователя и ``traceparent`` текущего запроса."""
    from opentelemetry.propagate import inject

    headers = _headers(user_id)
    inject(headers)
    return headers
