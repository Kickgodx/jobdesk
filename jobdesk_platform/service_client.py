"""HTTP-клиент между сервисами JobDesk: сервисный токен, тайм-аут, повторы, разбор ошибок.

Ошибка с телом в едином формате (``detail = {code, message, retryable}``) — ``UpstreamError``:
её статус и ``detail`` вызывающий отдаёт дальше как есть. Сеть, тайм-аут и ответ не по
контракту — ``UpstreamUnavailable`` (у ядра это 503 ``UPSTREAM_UNAVAILABLE``).

Повторы: ошибку установки соединения (запрос не ушёл) — всегда; прочие сетевые сбои и
502/503/504 — только для идемпотентных вызовов (``idempotent=True``), иначе повтор мог бы
выполнить действие дважды. ``traceparent`` текущего контекста клиент кладёт сам (инструментация
httpx, если включена, заменит его своим клиентским span'ом): трасса не рвётся, даже если
инструментация молча не включилась.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from opentelemetry.propagate import inject

_LOG = logging.getLogger(__name__)

_RETRY_STATUSES = frozenset({502, 503, 504})


class UpstreamUnavailable(RuntimeError):
    """Сервис не ответил или ответил не по контракту."""


class UpstreamError(RuntimeError):
    """Сервис ответил ошибкой в едином формате."""

    def __init__(self, status_code: int, detail: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {detail.get('code')}: {detail.get('message')}")
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


def contract_detail(response: httpx.Response) -> dict[str, Any] | None:
    """``detail`` ошибки в едином формате или ``None``, если тело не по контракту."""
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return {
            "code": str(detail["code"]),
            "message": str(detail["message"]),
            "retryable": bool(detail.get("retryable", False)),
        }
    return None


class ServiceClient:
    """Синхронный клиент одного сервиса; ``client`` — готовый ``httpx.Client`` (в тестах ``TestClient``)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        service: str,
        timeout: float = 10.0,
        attempts: int = 3,
        backoff_seconds: float = 0.2,
        client: httpx.Client | None = None,
    ) -> None:
        self.service = service
        self._token = token
        self._attempts = max(1, attempts)
        self._backoff = backoff_seconds
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._timeout = timeout

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ServiceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if extra:
            headers.update(extra)
        inject(headers)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = False,
    ) -> httpx.Response:
        """Ответ 2xx; иначе ``UpstreamError`` (ошибка по контракту) или ``UpstreamUnavailable``."""
        last_error: str = ""
        for attempt in range(1, self._attempts + 1):
            final = attempt == self._attempts
            try:
                response = self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=self._headers(headers),
                    timeout=self._timeout,
                )
            except httpx.ConnectError as e:
                last_error = f"соединение не установлено: {e}"
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}: {e}"
                if not idempotent:
                    raise UpstreamUnavailable(f"{self.service}: {last_error}") from e
            else:
                if response.status_code < 400:
                    return response
                if response.status_code in _RETRY_STATUSES and idempotent and not final:
                    last_error = f"HTTP {response.status_code}"
                else:
                    detail = contract_detail(response)
                    if detail is None:
                        raise UpstreamUnavailable(f"{self.service}: HTTP {response.status_code} не по контракту")
                    raise UpstreamError(response.status_code, detail, dict(response.headers))
            if final:
                break
            _LOG.info("%s %s %s: попытка %s не удалась (%s), повтор", self.service, method, path, attempt, last_error)
            time.sleep(self._backoff * (2 ** (attempt - 1)))
        raise UpstreamUnavailable(f"{self.service}: {last_error}")

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Как ``request``, но тело ответа — JSON; не-JSON 2xx — ``UpstreamUnavailable``."""
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as e:
            raise UpstreamUnavailable(f"{self.service}: ответ {method} {path} не JSON") from e
