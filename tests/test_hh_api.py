"""API HH через прокси ядра → HH-сервис: admin-only, 503 без токена, skip detail при повторном sync."""

from __future__ import annotations

import time
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from tests.conftest import clear_settings_caches, hh_service_stack


@pytest.fixture
def client(monkeypatch, tmp_path, pg_db):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
telegram:
  telethon_session_file: sessions/tg_jobs.session
sources:
  channels: [test_channel]
search:
  date_from: week_ago
  date_to: today
filters:
  specializations: [Python]
  exclude_keywords: []
  work_formats: [any]
ai:
  enabled: false
fx:
  enabled: false
hh:
  enabled: true
  listing_ttl_days: 40
  search:
    text: "Python AND AQA"
    text_no_python: "AQA OR SDET"
    professional_role: ["124", "96"]
    per_page: 100
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    # Пустые строки перекрывают значения из корневого .env (pydantic env_file).
    monkeypatch.setenv("HH_API_TOKEN", "")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "")

    from backend.db.session import reset_engine

    clear_settings_caches()
    reset_engine()

    from backend.main import app

    # Ядро и HH-сервис в одном процессе: /api/v1/hh/* проксируется в сервис, тот пишет в ядро.
    with TestClient(app) as c, hh_service_stack(monkeypatch):
        yield c


def _register(client: TestClient, email: str, password: str = "secret1") -> str:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait_job(client: TestClient, token: str, job_id: str, timeout: float = 10.0) -> dict:
    """Sync идёт фоновым потоком (inprocess): ждём completed/failed/cancelled."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/hh/sync-jobs/{job_id}", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"HH sync {job_id} не завершился за {timeout} с")


def _sync(client: TestClient, token: str, **kwargs) -> dict:
    r = client.post("/api/v1/hh/sync", headers=_auth(token), **kwargs)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    return _wait_job(client, token, r.json()["job_id"])


def test_hh_status_requires_admin(client: TestClient):
    admin = _register(client, "admin@example.com")
    user = _register(client, "user@example.com")
    assert client.get("/api/v1/hh/status", headers=_auth(user)).status_code == 403
    r = client.get("/api/v1/hh/status", headers=_auth(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["token_configured"] is False
    assert body["search_text"] == "Python AND AQA"
    assert body["search_text_no_python"] == "AQA OR SDET"


def test_hh_sync_503_without_token(client: TestClient):
    from prometheus_client import REGISTRY

    admin = _register(client, "admin2@example.com")
    before = REGISTRY.get_sample_value("api_overload_rejections_total") or 0.0
    r = client.post("/api/v1/hh/sync", headers=_auth(admin))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] != "OVERLOADED"
    # Алерт перегрузки не должен видеть 503 от HH.
    assert (REGISTRY.get_sample_value("api_overload_rejections_total") or 0.0) == before


def test_hh_sync_skip_detail_on_second_run(client: TestClient, monkeypatch):
    monkeypatch.setenv("HH_API_TOKEN", "test-token")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "dev@example.com")
    clear_settings_caches()

    admin = _register(client, "admin3@example.com")

    short_payload = {
        "id": "100",
        "name": "AQA",
        "alternate_url": "https://hh.ru/vacancy/100",
        "area": {"name": "Москва"},
        "employer": {"id": "1", "name": "ACME"},
        "snippet": {"requirement": "Python", "responsibility": "tests"},
        "work_format": [{"id": "REMOTE"}],
        "salary_range": {"from": 100, "to": 200, "currency": "RUR", "gross": False},
        "experience": {"id": "between1And3", "name": "1-3"},
    }
    detail_payload = {
        **short_payload,
        "description": "<p>Full description with pytest</p>",
        "key_skills": [{"name": "pytest"}],
        "published_at": "2026-10-01T10:00:00+0300",
    }

    search_page = MagicMock()
    search_page.found = 1
    search_page.items = [
        __import__("jobdesk.hh.models", fromlist=["VacancyShort"]).VacancyShort.model_validate(short_payload)
    ]

    detail = __import__("jobdesk.hh.models", fromlist=["VacancyDetail"]).VacancyDetail.model_validate(detail_payload)

    detail_calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

        def iter_vacancy_pages(self, *a, **k):
            yield search_page

        def get_vacancy(self, vacancy_id: str):
            detail_calls["n"] += 1
            assert vacancy_id == "100"
            return detail

    monkeypatch.setattr("jobdesk_hh.sync.HHClient", FakeClient)

    job1 = _sync(client, admin)
    assert job1["status"] == "completed", job1
    assert job1["progress"]["phase"] == "purge"
    body1 = job1["result"]
    assert body1["details_fetched"] == 1
    assert body1["details_skipped"] == 0
    assert body1["vacancies_inserted"] >= 1
    assert detail_calls["n"] == 1

    job2 = _sync(client, admin)
    assert job2["status"] == "completed", job2
    body2 = job2["result"]
    assert body2["details_skipped"] == 1
    assert body2["details_fetched"] == 0
    assert detail_calls["n"] == 1  # второй sync не дергал detail


def test_hh_sync_passes_period_for_7d(client: TestClient, monkeypatch):
    monkeypatch.setenv("HH_API_TOKEN", "test-token")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "dev@example.com")
    clear_settings_caches()

    captured: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

        def iter_vacancy_pages(self, params):
            captured["params"] = params
            return iter([])

    monkeypatch.setattr("jobdesk_hh.sync.HHClient", FakeClient)

    admin = _register(client, "admin4@example.com")
    job = _sync(client, admin, json={"window": "7d"})
    assert job["status"] == "completed", job
    assert job["window"] == "7d"
    assert job["period"] == 7
    assert job["result"]["period"] == 7
    assert captured["params"].period == 7
    assert captured["params"].date_from is None


def test_hh_sync_range_422_without_dates(client: TestClient, monkeypatch):
    monkeypatch.setenv("HH_API_TOKEN", "test-token")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "dev@example.com")
    clear_settings_caches()

    admin = _register(client, "admin5@example.com")
    r = client.post(
        "/api/v1/hh/sync",
        headers=_auth(admin),
        json={"window": "range"},
    )
    assert r.status_code == 422


def _configure_hh(monkeypatch, client_cls) -> None:
    monkeypatch.setenv("HH_API_TOKEN", "test-token")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "dev@example.com")
    clear_settings_caches()
    monkeypatch.setattr("jobdesk_hh.sync.HHClient", client_cls)


class _EmptyClient:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass

    def iter_vacancy_pages(self, params):
        return iter([])


def test_hh_sync_conflict_while_job_is_active(client: TestClient, monkeypatch):
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin6@example.com")
    from jobdesk_hh.db import jobs
    from jobdesk_hh.db.session import session_scope

    with session_scope() as session:
        jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None)

    r = client.post("/api/v1/hh/sync", headers=_auth(admin))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CONFLICT_ACTIVE_RUN"


def test_hh_sync_idempotency_key_returns_same_job(client: TestClient, monkeypatch):
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin7@example.com")
    headers = {**_auth(admin), "Idempotency-Key": "sync-1"}

    r1 = client.post("/api/v1/hh/sync", headers=headers, json={"window": "7d"})
    assert r1.status_code == 202, r1.text
    _wait_job(client, admin, r1.json()["job_id"])
    r2 = client.post("/api/v1/hh/sync", headers=headers, json={"window": "7d"})

    assert r2.status_code == 202
    assert r2.json()["job_id"] == r1.json()["job_id"]
    jobs = client.get("/api/v1/hh/sync-jobs", headers=_auth(admin)).json()
    assert [j["id"] for j in jobs] == [r1.json()["job_id"]]


def test_hh_sync_api_error_fails_job(client: TestClient, monkeypatch):
    from jobdesk.hh.client import HHApiError

    class FailingClient(_EmptyClient):
        def iter_vacancy_pages(self, params):
            raise HHApiError("bad gateway", status_code=502)

    _configure_hh(monkeypatch, FailingClient)
    admin = _register(client, "admin8@example.com")

    job = _sync(client, admin)

    assert job["status"] == "failed"
    assert job["error_message"] == "HH API error: HTTP 502"
    assert job["result"]["searched"] == 0  # частичный итог на момент сбоя
    # Упавший sync не держит активный слот: следующий запускается.
    _configure_hh(monkeypatch, _EmptyClient)
    assert _sync(client, admin)["status"] == "completed"


def test_hh_sync_jobs_are_admin_only(client: TestClient, monkeypatch):
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin9@example.com")
    user = _register(client, "user9@example.com")
    job = _sync(client, admin)

    assert client.get("/api/v1/hh/sync-jobs", headers=_auth(user)).status_code == 403
    assert client.get(f"/api/v1/hh/sync-jobs/{job['id']}", headers=_auth(user)).status_code == 403
    ticket = client.post("/api/v1/auth/sse-ticket", headers=_auth(user)).json()["ticket"]
    assert client.get(f"/api/v1/hh/sync-jobs/{job['id']}/events?ticket={ticket}").status_code == 403
    assert client.post("/api/v1/hh/sync", headers=_auth(user)).status_code == 403


def test_hh_sync_job_events_stream_status_then_done(client: TestClient, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin10@example.com")
    job = _sync(client, admin)

    ticket = client.post("/api/v1/auth/sse-ticket", headers=_auth(admin)).json()["ticket"]
    with client.stream("GET", f"/api/v1/hh/sync-jobs/{job['id']}/events?ticket={ticket}") as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())

    assert "event: status" in text
    assert '"status":"completed"' in text
    assert text.rstrip().endswith("data: {}")  # завершающий event: done


def _queued_sync_job() -> str:
    from jobdesk_hh.db import jobs as hh_jobs
    from jobdesk_hh.db.session import session_scope

    with session_scope() as session:
        return str(hh_jobs.create_hh_sync_job(session, params={"window": "all"}, requested_by=None).id)


def test_hh_sync_cancel_api(client: TestClient, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    admin = _register(client, "admin11@example.com")
    user = _register(client, "user11@example.com")
    job_id = _queued_sync_job()

    assert client.post(f"/api/v1/hh/sync-jobs/{job_id}/cancel", headers=_auth(user)).status_code == 403
    r = client.post(f"/api/v1/hh/sync-jobs/{job_id}/cancel", headers=_auth(admin))
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("cancelled", True)

    ticket = client.post("/api/v1/auth/sse-ticket", headers=_auth(admin)).json()["ticket"]
    with client.stream("GET", f"/api/v1/hh/sync-jobs/{job_id}/events?ticket={ticket}") as resp:
        text = "".join(resp.iter_text())
    assert '"status":"cancelled"' in text
    assert text.rstrip().endswith("data: {}")


def test_hh_sync_cancel_of_completed_job_returns_current_state(client: TestClient, monkeypatch):
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin12@example.com")
    job = _sync(client, admin)

    r = client.post(f"/api/v1/hh/sync-jobs/{job['id']}/cancel", headers=_auth(admin))

    assert r.status_code == 200
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("completed", False)
    assert (
        client.post(
            "/api/v1/hh/sync-jobs/00000000-0000-0000-0000-000000000000/cancel", headers=_auth(admin)
        ).status_code
        == 404
    )


def _short(hh_id: str, name: str, employer: str):
    from jobdesk.hh.models import VacancyShort

    return VacancyShort.model_validate(
        {
            "id": hh_id,
            "name": name,
            "alternate_url": f"https://hh.ru/vacancy/{hh_id}",
            "area": {"name": "Москва"},
            "employer": {"id": hh_id, "name": employer},
            "snippet": {"requirement": "Python", "responsibility": "tests"},
        }
    )


def _detail(short):
    from jobdesk.hh.models import VacancyDetail

    return VacancyDetail.model_validate(
        {
            **short.model_dump(mode="json"),
            "description": f"<p>{short.name} — pytest</p>",
            "published_at": "2026-10-01T10:00:00+0300",
        }
    )


def _request_cancel_of_active_sync() -> None:
    from jobdesk_hh.db import jobs as hh_jobs
    from jobdesk_hh.db.session import session_scope

    with session_scope() as session:
        active = next(j for j in hh_jobs.list_hh_sync_jobs(session, limit=5) if j.status == "running")
        hh_jobs.request_hh_sync_job_cancel(session, active.id)


def _hh_token(monkeypatch):
    from jobdesk_hh import tasks as hh_sync_tasks

    monkeypatch.setenv("HH_API_TOKEN", "test-token")
    monkeypatch.setenv("HH_CONTACT_EMAIL", "dev@example.com")
    monkeypatch.setenv("REDIS_URL", "")
    clear_settings_caches()
    # Флаг отмены — на каждой проверке, без секундного троттлинга.
    monkeypatch.setattr(hh_sync_tasks, "_CANCEL_CHECK_INTERVAL_S", 0.0)


def test_hh_sync_cancel_during_details_keeps_fetched(client: TestClient, monkeypatch):
    _hh_token(monkeypatch)
    shorts = [_short("201", "AQA Python", "ACME"), _short("202", "Backend Go", "Globex")]
    page = MagicMock(found=2, items=shorts)
    fetched: list[str] = []

    class FakeClient(_EmptyClient):
        def iter_vacancy_pages(self, *a, **k):
            yield page

        def get_vacancy(self, vacancy_id: str):
            fetched.append(vacancy_id)
            _request_cancel_of_active_sync()  # отмена пришла, пока грузилась первая группа
            return _detail(next(s for s in shorts if s.id == vacancy_id))

    monkeypatch.setattr("jobdesk_hh.sync.HHClient", FakeClient)
    admin = _register(client, "admin13@example.com")

    job = _sync(client, admin)

    assert (job["status"], job["cancel_requested"]) == ("cancelled", True)
    assert len(fetched) == 1  # детали второй группы не запрашивались
    assert job["result"]["details_fetched"] == 1
    assert job["result"]["vacancies_inserted"] == 1
    assert job["result"]["listings_purged"] == 0


def test_hh_sync_cancel_during_search_stops_paging(client: TestClient, monkeypatch):
    _hh_token(monkeypatch)
    pages_requested: list[int] = []

    class FakeClient(_EmptyClient):
        def iter_vacancy_pages(self, *a, **k):
            for n in (1, 2, 3):
                pages_requested.append(n)
                if n == 1:
                    _request_cancel_of_active_sync()
                yield MagicMock(found=3, items=[_short(f"30{n}", f"AQA {n}", f"Co{n}")])

        def get_vacancy(self, vacancy_id: str):
            raise AssertionError("после отмены детали не запрашиваются")

    monkeypatch.setattr("jobdesk_hh.sync.HHClient", FakeClient)
    admin = _register(client, "admin14@example.com")

    job = _sync(client, admin)

    assert job["status"] == "cancelled"
    assert pages_requested == [1]
    assert (job["result"]["pages"], job["result"]["details_fetched"]) == (1, 0)


def _pages(found: int, sizes: list[int]):
    for n, size in enumerate(sizes):
        yield MagicMock(found=found, items=[_short(f"9{n}{i:03d}", f"AQA {n}-{i}", f"Co{n}-{i}") for i in range(size)])


def test_collect_shorts_warns_when_hh_found_more_than_depth_limit():
    from jobdesk.config.schema import HhConfig
    from jobdesk_hh.sync import _collect_shorts

    client = MagicMock()
    client.iter_vacancy_pages.return_value = _pages(5000, [100] * 20)
    warnings: list[str] = []

    shorts, pages, stopped = _collect_shorts(client, HhConfig(), warnings=warnings)

    assert (len(shorts), pages, stopped) == (2000, 20, False)
    assert len(warnings) == 1
    assert "5000" in warnings[0] and "2000" in warnings[0]


def test_collect_shorts_within_limit_has_no_warning():
    from jobdesk.config.schema import HhConfig
    from jobdesk_hh.sync import _collect_shorts

    client = MagicMock()
    client.iter_vacancy_pages.return_value = _pages(150, [100, 50])
    warnings: list[str] = []

    shorts, pages, _ = _collect_shorts(client, HhConfig(), warnings=warnings)

    assert (len(shorts), pages) == (150, 2)
    assert warnings == []


def test_hh_sync_result_carries_depth_warning(client: TestClient, monkeypatch):
    _hh_token(monkeypatch)
    short = _short("401", "AQA Python", "ACME")

    class FakeClient(_EmptyClient):
        def iter_vacancy_pages(self, *a, **k):
            yield MagicMock(found=5000, items=[short])

        def get_vacancy(self, vacancy_id: str):
            return _detail(short)

    monkeypatch.setattr("jobdesk_hh.sync.HHClient", FakeClient)
    admin = _register(client, "admin40@example.com")

    job = _sync(client, admin)

    assert job["status"] == "completed", job
    assert len(job["result"]["warnings"]) == 1
    assert "5000" in job["result"]["warnings"][0]


class _CapturingClient(_EmptyClient):
    texts: ClassVar[list[str]] = []

    def iter_vacancy_pages(self, params):
        _CapturingClient.texts.append(params.text)
        return iter([])


@pytest.mark.parametrize(
    ("body", "query", "text"),
    [
        ({}, "main", "Python AND AQA"),
        ({"query": "no_python"}, "no_python", "AQA OR SDET"),
        ({"query": "custom", "text": "  Go AND backend  "}, "custom", "Go AND backend"),
    ],
)
def test_hh_sync_searches_by_chosen_query(client: TestClient, monkeypatch, body, query, text):
    _CapturingClient.texts = []
    _configure_hh(monkeypatch, _CapturingClient)
    admin = _register(client, f"admin-q-{query}@example.com")

    job = _sync(client, admin, json={"window": "7d", **body})

    assert job["status"] == "completed", job
    assert _CapturingClient.texts == [text]
    assert (job["query"], job["search_text"]) == (query, text)
    assert (job["result"]["query"], job["result"]["search_text"]) == (query, text)


@pytest.mark.parametrize(
    "body",
    [
        {"query": "custom"},
        {"query": "custom", "text": "   "},
        {"query": "custom", "text": "x" * 1001},
        {"query": "main", "text": "Go"},
        {"query": "java"},
    ],
)
def test_hh_sync_invalid_query_is_422(client: TestClient, monkeypatch, body):
    _configure_hh(monkeypatch, _EmptyClient)
    admin = _register(client, "admin-q-bad@example.com")

    r = client.post("/api/v1/hh/sync", headers=_auth(admin), json=body)

    assert r.status_code == 422, r.text
    assert client.get("/api/v1/hh/sync-jobs", headers=_auth(admin)).json() == []
