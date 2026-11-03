"""Сервисный токен и ServiceClient: повторы, разбор detail, ответы не по контракту."""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jobdesk_platform.errors import register_exception_handlers
from jobdesk_platform.service_auth import require_service_token, tokens_match
from jobdesk_platform.service_client import ServiceClient, UpstreamError, UpstreamUnavailable

_DETAIL = {"code": "CONFLICT_ACTIVE_RUN", "message": "уже идёт", "retryable": False}


def _client(handler, **kwargs) -> tuple[ServiceClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    http = httpx.Client(base_url="http://svc", transport=httpx.MockTransport(record))
    return ServiceClient("http://svc", "tok", service="svc", backoff_seconds=0, client=http, **kwargs), seen


# --- токен -------------------------------------------------------------------------------


def _app(expected: str | None) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/internal", dependencies=[Depends(require_service_token(lambda: expected))])
    def internal() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_service_token_accepts_matching_bearer():
    assert _app("secret").get("/internal", headers={"Authorization": "Bearer secret"}).json() == {"ok": True}


@pytest.mark.parametrize("header", [None, "Bearer other", "Basic secret", "Bearer "])
def test_service_token_rejects_missing_or_wrong(header):
    headers = {"Authorization": header} if header is not None else {}
    resp = _app("secret").get("/internal", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "NOT_AUTHENTICATED"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_empty_expected_token_matches_nothing():
    assert not tokens_match("", "")
    assert _app(None).get("/internal", headers={"Authorization": "Bearer x"}).status_code == 401


# --- клиент ------------------------------------------------------------------------------


def test_sends_bearer_and_extra_headers():
    client, seen = _client(lambda req, n: httpx.Response(200, json={"ok": 1}))
    assert client.request_json("POST", "/v1/x", json={"a": 1}, headers={"X-JobDesk-User-Id": "7"}) == {"ok": 1}
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].headers["x-jobdesk-user-id"] == "7"


def test_contract_error_passes_status_and_detail():
    client, seen = _client(lambda req, n: httpx.Response(409, json={"detail": _DETAIL}))
    with pytest.raises(UpstreamError) as exc:
        client.request("POST", "/v1/sync", idempotent=True)
    assert exc.value.status_code == 409
    assert exc.value.detail == _DETAIL
    assert len(seen) == 1  # 4xx не повторяется


def test_idempotent_call_retries_503_then_succeeds():
    client, seen = _client(lambda req, n: httpx.Response(503) if n < 3 else httpx.Response(200, json={}))
    assert client.request_json("GET", "/v1/status", idempotent=True) == {}
    assert len(seen) == 3


def test_last_503_with_contract_detail_is_upstream_error():
    detail = {"code": "UPSTREAM_UNAVAILABLE", "message": "HH выключен", "retryable": True}
    client, seen = _client(lambda req, n: httpx.Response(503, json={"detail": detail}))
    with pytest.raises(UpstreamError) as exc:
        client.request("GET", "/v1/status", idempotent=True)
    assert exc.value.status_code == 503
    assert len(seen) == 3


def test_non_idempotent_503_not_retried():
    client, seen = _client(lambda req, n: httpx.Response(503, text="bad gateway"))
    with pytest.raises(UpstreamUnavailable):
        client.request("POST", "/v1/sync")
    assert len(seen) == 1


def test_connect_error_retried_even_when_not_idempotent():
    def handler(req, n):
        if n < 2:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(202, json={"job_id": "x"})

    client, seen = _client(handler)
    assert client.request_json("POST", "/v1/sync") == {"job_id": "x"}
    assert len(seen) == 2


def test_read_timeout_not_retried_for_non_idempotent():
    def handler(req, n):
        raise httpx.ReadTimeout("slow", request=req)

    client, seen = _client(handler)
    with pytest.raises(UpstreamUnavailable):
        client.request("POST", "/v1/sync")
    assert len(seen) == 1


def test_connect_error_exhausts_attempts():
    def handler(req, n):
        raise httpx.ConnectError("refused", request=req)

    client, seen = _client(handler, attempts=2)
    with pytest.raises(UpstreamUnavailable, match="соединение не установлено"):
        client.request("GET", "/v1/status", idempotent=True)
    assert len(seen) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(404, json={"detail": "Not Found"}),
    ],
)
def test_error_without_contract_is_unavailable(response):
    client, _ = _client(lambda req, n: response)
    with pytest.raises(UpstreamUnavailable, match="не по контракту"):
        client.request("GET", "/v1/status")


def test_non_json_success_body_is_unavailable():
    client, _ = _client(lambda req, n: httpx.Response(200, text="ok"))
    with pytest.raises(UpstreamUnavailable, match="не JSON"):
        client.request_json("GET", "/v1/status")
