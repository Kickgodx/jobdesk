"""Прокси /api/v1/hh/* в ядре (platform/internal-services): недоступный сервис, контракт, права."""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import clear_settings_caches, login_as_admin

from backend.services import hh_service_client


@pytest.fixture
def no_overrides():
    hh_service_client.set_client_overrides(None)
    yield
    hh_service_client.set_client_overrides(None)


def _register_user(client, email: str = "user@example.com") -> str:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "secret1"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _assert_unavailable(resp) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert resp.json()["detail"]["retryable"] is True


def test_empty_service_url_is_503_and_core_stays_healthy(api_client, monkeypatch, no_overrides):
    monkeypatch.setenv("HH_SERVICE_URL", "")
    clear_settings_caches()
    login_as_admin(api_client)

    _assert_unavailable(api_client.get("/api/v1/hh/status"))
    _assert_unavailable(api_client.post("/api/v1/hh/sync", json={"window": "7d"}))
    assert api_client.get("/api/v1/health").json()["status"] == "ok"


def test_stopped_service_is_503(api_client, no_overrides):
    # Сервис не слушает порт: соединение отклоняется (ConnectError — и после повторов клиента).
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    hh_service_client.set_client_overrides(
        httpx.Client(base_url="http://hh-service", transport=httpx.MockTransport(refuse)),
        lambda: httpx.AsyncClient(base_url="http://hh-service", transport=httpx.MockTransport(refuse)),
    )
    login_as_admin(api_client)

    _assert_unavailable(api_client.get("/api/v1/hh/sync-jobs"))
    ticket = api_client.post("/api/v1/auth/sse-ticket").json()["ticket"]
    _assert_unavailable(
        api_client.get(f"/api/v1/hh/sync-jobs/00000000-0000-0000-0000-000000000000/events?ticket={ticket}")
    )


def test_response_out_of_contract_is_503(api_client, no_overrides):
    garbage = httpx.Client(
        base_url="http://hh-service", transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"x": 1}))
    )
    hh_service_client.set_client_overrides(garbage)
    login_as_admin(api_client)
    _assert_unavailable(api_client.get("/api/v1/hh/status"))


def test_service_error_detail_passes_through(api_client, no_overrides):
    detail = {"code": "CONFLICT_ACTIVE_RUN", "message": "HH sync уже выполняется.", "retryable": False}
    conflict = httpx.Client(
        base_url="http://hh-service",
        transport=httpx.MockTransport(lambda req: httpx.Response(409, json={"detail": detail})),
    )
    hh_service_client.set_client_overrides(conflict)
    login_as_admin(api_client)
    resp = api_client.post("/api/v1/hh/sync", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"] == detail


def test_user_and_id_header_reach_service_only_for_admin(api_client, no_overrides):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202, json={"job_id": "00000000-0000-0000-0000-000000000001", "status": "queued"})

    hh_service_client.set_client_overrides(
        httpx.Client(base_url="http://hh-service", transport=httpx.MockTransport(handler))
    )
    login_as_admin(api_client)
    user = _register_user(api_client)

    denied = api_client.post("/api/v1/hh/sync", json={}, headers={"Authorization": f"Bearer {user}"})
    assert denied.status_code == 403
    assert seen == []  # до сервиса запрос не дошёл

    assert api_client.post("/api/v1/hh/sync", json={"window": "7d"}).status_code == 202
    (req,) = seen
    assert req.headers["x-jobdesk-user-id"] == "1"
    assert req.headers["authorization"].startswith("Bearer ")
    assert req.headers["authorization"] != api_client.headers["Authorization"]  # сервисный токен, не JWT


def test_unknown_sync_events_404_through_proxy(api_client, hh_client):
    login_as_admin(api_client)
    ticket = api_client.post("/api/v1/auth/sse-ticket").json()["ticket"]
    resp = api_client.get(f"/api/v1/hh/sync-jobs/00000000-0000-0000-0000-000000000000/events?ticket={ticket}")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"
