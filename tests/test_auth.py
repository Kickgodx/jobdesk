"""JWT-auth: register/login/me, per-user скоуп резюме и пресетов, legacy-claim."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    from backend.db.session import reset_engine
    from backend.settings import get_settings

    get_settings.cache_clear()
    reset_engine()

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _register(client: TestClient, email: str, password: str = "secret1"):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_login_me(client: TestClient):
    r = _register(client, "First@Example.com")
    assert r.status_code == 201
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["email"] == "first@example.com"
    assert body["user"]["is_admin"] is True  # первый пользователь — админ
    token = body["access_token"]

    r = client.get("/api/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["email"] == "first@example.com"

    # второй пользователь — не админ
    r2 = _register(client, "second@example.com")
    assert r2.status_code == 201
    assert r2.json()["user"]["is_admin"] is False

    # дубликат email → 409
    assert _register(client, "first@example.com").status_code == 409

    # логин: неверный пароль → 401, верный → 200
    bad = client.post(
        "/api/v1/auth/login",
        json={"email": "first@example.com", "password": "wrong-pass"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/api/v1/auth/login",
        json={"email": "first@example.com", "password": "secret1"},
    )
    assert ok.status_code == 200
    assert ok.json()["access_token"]


def test_me_requires_token(client: TestClient):
    assert client.get("/api/v1/auth/me").status_code == 401
    r = client.get("/api/v1/auth/me", headers=_auth("garbage"))
    assert r.status_code == 401


def test_token_form_login_for_swagger(client: TestClient):
    """OAuth2 password flow (кнопка Authorize в Swagger): urlencoded, email в username."""
    _register(client, "Swagger@Example.com")
    url = "/api/v1/auth/token"
    form = {"grant_type": "password", "username": "swagger@example.com", "password": "wrong-pass"}
    assert client.post(url, data=form).status_code == 401
    assert client.post(url, data={"username": "swagger@example.com"}).status_code == 422

    r = client.post(url, data={**form, "password": "secret1"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert client.get("/api/v1/auth/me", headers=_auth(body["access_token"])).status_code == 200


def test_openapi_declares_auth_schemes(client: TestClient):
    """Без security-схем Swagger UI не шлёт Authorization (header-параметр он игнорирует)."""
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/api/v1/auth/token"
    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"
    me = spec["paths"]["/api/v1/auth/me"]["get"]
    assert {"HTTPBearer": []} in me["security"]
    assert all(p["name"].lower() != "authorization" for p in me.get("parameters", []))


def test_resumes_are_per_user(client: TestClient):
    t1 = _register(client, "a@example.com").json()["access_token"]
    t2 = _register(client, "b@example.com").json()["access_token"]

    assert client.get("/api/v1/resumes").status_code == 401
    assert client.get("/api/v1/resumes", headers=_auth(t1)).json()["items"] == []

    r = client.post(
        "/api/v1/resumes",
        headers=_auth(t1),
        json={"name": "CV A", "text": "python dev"},
    )
    assert r.status_code == 201
    rid = r.json()["id"]

    # второй пользователь не видит чужое резюме
    assert client.get("/api/v1/resumes", headers=_auth(t2)).json()["items"] == []
    assert client.get(f"/api/v1/resumes/{rid}", headers=_auth(t2)).status_code == 404
    assert client.delete(f"/api/v1/resumes/{rid}", headers=_auth(t2)).status_code == 404
    # вакансии с чужим resume_id → 404, без токена → 401
    assert client.get(f"/api/v1/vacancies?resume_id={rid}").status_code == 401
    assert client.get(f"/api/v1/vacancies?resume_id={rid}", headers=_auth(t2)).status_code == 404
    assert client.get(f"/api/v1/vacancies?resume_id={rid}", headers=_auth(t1)).status_code == 200

    assert client.delete(f"/api/v1/resumes/{rid}", headers=_auth(t1)).status_code == 204


def test_resume_update(client: TestClient):
    t1 = _register(client, "a@example.com").json()["access_token"]
    t2 = _register(client, "b@example.com").json()["access_token"]

    rid = client.post(
        "/api/v1/resumes",
        headers=_auth(t1),
        json={"name": "CV", "text": "old text"},
    ).json()["id"]

    assert client.put(f"/api/v1/resumes/{rid}", headers=_auth(t2), json={"name": "x"}).status_code == 404
    assert client.put(f"/api/v1/resumes/{rid}", json={"name": "x"}).status_code == 401

    r = client.put(
        f"/api/v1/resumes/{rid}",
        headers=_auth(t1),
        json={"name": "CV v2", "text": "new text"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "CV v2"
    assert r.json()["text"] == "new text"

    r = client.put(f"/api/v1/resumes/{rid}", headers=_auth(t1), json={"text": "newer text"})
    assert r.status_code == 200
    assert r.json()["name"] == "CV v2"
    assert r.json()["text"] == "newer text"


def test_saved_filters_are_per_user(client: TestClient):
    t1 = _register(client, "a@example.com").json()["access_token"]
    t2 = _register(client, "b@example.com").json()["access_token"]

    r = client.post(
        "/api/v1/filters",
        headers=_auth(t1),
        json={"name": "preset", "params": {"q": "python"}},
    )
    assert r.status_code == 201
    fid = r.json()["id"]

    assert client.get("/api/v1/filters", headers=_auth(t1)).json()["items"][0]["name"] == "preset"
    assert client.get("/api/v1/filters", headers=_auth(t2)).json()["items"] == []
    assert client.delete(f"/api/v1/filters/{fid}", headers=_auth(t2)).status_code == 404
    assert client.delete(f"/api/v1/filters/{fid}", headers=_auth(t1)).status_code == 204


def test_first_user_claims_legacy_rows(client: TestClient):
    from backend.db.models import ResumeRow, SavedFilterRow
    from backend.db.session import get_session_factory

    factory = get_session_factory()
    session = factory()
    try:
        session.add(ResumeRow(name="legacy", text="old resume", user_id=None))
        session.add(SavedFilterRow(name="legacy preset", params={}, user_id=None))
        session.commit()
    finally:
        session.close()

    token = _register(client, "owner@example.com").json()["access_token"]

    resumes = client.get("/api/v1/resumes", headers=_auth(token)).json()["items"]
    assert [r["name"] for r in resumes] == ["legacy"]
    filters = client.get("/api/v1/filters", headers=_auth(token)).json()["items"]
    assert [f["name"] for f in filters] == ["legacy preset"]

    # второй пользователь legacy-строк не видит
    t2 = _register(client, "other@example.com").json()["access_token"]
    assert client.get("/api/v1/resumes", headers=_auth(t2)).json()["items"] == []


def test_registration_closed_for_new_users(client: TestClient, monkeypatch):
    monkeypatch.setenv("AUTH_REGISTRATION_OPEN", "false")
    from backend.settings import get_settings

    get_settings.cache_clear()
    try:
        # первый пользователь регистрируется всегда (bootstrap)
        assert _register(client, "admin@example.com").status_code == 201
        assert _register(client, "second@example.com").status_code == 403
    finally:
        get_settings.cache_clear()


def test_registration_closed_by_default_in_prod(client: TestClient, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "test-secret-value-for-tests-32-chars")
    monkeypatch.setenv("CORE_INTERNAL_TOKEN", "test-core-internal-token-32-chars-x")
    monkeypatch.delenv("AUTH_REGISTRATION_OPEN", raising=False)
    from backend.settings import get_settings

    get_settings.cache_clear()
    try:
        assert _register(client, "admin@example.com").status_code == 201
        assert _register(client, "second@example.com").status_code == 403
    finally:
        get_settings.cache_clear()


def _login(client: TestClient, email: str, password: str = "secret1"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


@pytest.fixture
def auth_limits(monkeypatch):
    """Включить лимиты /auth (в conftest они выключены) на чистых счётчиках и фиксированном окне."""
    from backend.services import auth_rate_limit
    from backend.settings import get_settings

    def _set(*, per_minute: int = 0, max_failed: int = 0) -> None:
        monkeypatch.setenv("AUTH_RATE_LIMIT_PER_MINUTE", str(per_minute))
        monkeypatch.setenv("AUTH_MAX_FAILED_LOGINS", str(max_failed))
        get_settings.cache_clear()

    # Окно не перещёлкивается посреди теста — иначе счётчик обнулится и тест моргнёт.
    monkeypatch.setattr(auth_rate_limit, "_window", lambda window_s: (1, 42.0))
    auth_rate_limit.reset_auth_rate_limits()
    yield _set
    auth_rate_limit.reset_auth_rate_limits()
    get_settings.cache_clear()


def test_register_rejects_invalid_email(client: TestClient):
    r = _register(client, "not-an-email")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION"
    assert _register(client, "user@localhost").status_code == 422


def test_login_accepts_legacy_email_without_validation(client: TestClient):
    """Пользователи, заведённые до проверки формата email, входят как раньше."""
    from backend.db import user_content_repository as users_repo
    from backend.db.session import session_scope
    from backend.services.auth_service import hash_password

    with session_scope() as s:
        users_repo.create_user(s, email="admin", password_hash=hash_password("secret1"), is_admin=True)
    assert _login(client, "admin").status_code == 200


def test_register_duplicate_race_is_conflict_not_500(client: TestClient, monkeypatch):
    """Параллельная регистрация того же email: проверка прошла, UNIQUE сработал на вставке."""
    from backend.db import user_content_repository as users_repo

    assert _register(client, "race@example.com").status_code == 201
    monkeypatch.setattr(users_repo, "get_user_by_email", lambda session, email: None)
    r = _register(client, "race@example.com")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EMAIL_TAKEN"


def test_failed_logins_lock_email_before_password_check(client: TestClient, auth_limits, monkeypatch):
    auth_limits(max_failed=2)
    assert _register(client, "victim@example.com").status_code == 201
    assert _login(client, "victim@example.com", "wrong-1").status_code == 401
    assert _login(client, "Victim@Example.com", "wrong-2").status_code == 401

    from backend.services import auth_service

    verified = []
    monkeypatch.setattr(auth_service, "verify_password", lambda *a: verified.append(1) or True)
    r = _login(client, "victim@example.com", "secret1")
    assert r.status_code == 429
    assert r.json()["detail"] == {
        "code": "RATE_LIMITED",
        "message": r.json()["detail"]["message"],
        "retryable": True,
    }
    assert r.headers["Retry-After"] == "42"
    assert verified == []  # argon2 не вызывался
    # Другой email не задет.
    assert _register(client, "other@example.com").status_code == 201


def test_successful_login_resets_failures(client: TestClient, auth_limits):
    auth_limits(max_failed=2)
    assert _register(client, "u@example.com").status_code == 201
    assert _login(client, "u@example.com", "wrong-pass").status_code == 401
    assert _login(client, "u@example.com").status_code == 200
    assert _login(client, "u@example.com", "wrong-pass").status_code == 401
    assert _login(client, "u@example.com").status_code == 200


def test_auth_attempts_limited_per_ip(client: TestClient, auth_limits):
    auth_limits(per_minute=3)
    assert _register(client, "ip@example.com").status_code == 201
    assert _login(client, "ip@example.com").status_code == 200
    assert _login(client, "ip@example.com", "wrong-pass").status_code == 401
    r = _login(client, "ip@example.com")
    assert r.status_code == 429
    assert _register(client, "ip2@example.com").status_code == 429


def test_failed_logins_counted_in_redis(client: TestClient, auth_limits, monkeypatch):
    """С REDIS_URL счётчики общие для процессов API — лежат в Redis, а не в памяти."""
    import fakeredis

    from backend.services import auth_rate_limit

    server = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(auth_rate_limit, "_redis", lambda: server)
    auth_limits(max_failed=1)
    assert _register(client, "r@example.com").status_code == 201
    assert _login(client, "r@example.com", "wrong-pass").status_code == 401
    assert int(server.get("auth:rate:fail:r@example.com:1")) == 1
    assert _login(client, "r@example.com").status_code == 429
    auth_rate_limit.reset_auth_rate_limits()  # память пуста — лимит держит Redis
    assert _login(client, "r@example.com").status_code == 429


def test_change_password_revokes_old_tokens(client: TestClient):
    old = _register(client, "cp@example.com").json()["access_token"]
    second_session = _login(client, "cp@example.com").json()["access_token"]

    bad = client.post(
        "/api/v1/auth/change-password",
        headers=_auth(old),
        json={"current_password": "wrong", "new_password": "secret2"},
    )
    assert bad.status_code == 400
    assert client.get("/api/v1/auth/me", headers=_auth(old)).status_code == 200

    r = client.post(
        "/api/v1/auth/change-password",
        headers=_auth(old),
        json={"current_password": "secret1", "new_password": "secret2"},
    )
    assert r.status_code == 200
    new = r.json()["access_token"]

    assert client.get("/api/v1/auth/me", headers=_auth(old)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_auth(second_session)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_auth(new)).status_code == 200
    assert _login(client, "cp@example.com", "secret1").status_code == 401
    assert _login(client, "cp@example.com", "secret2").status_code == 200


def test_logout_all_revokes_tokens(client: TestClient):
    token = _register(client, "out@example.com").json()["access_token"]
    other = _register(client, "keep@example.com").json()["access_token"]
    assert client.post("/api/v1/auth/logout-all", headers=_auth(token)).status_code == 204
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 401
    # SSE-токен из query отозван так же.
    assert (
        client.get(
            "/api/v1/llm-jobs/00000000-0000-0000-0000-000000000000/events", params={"access_token": token}
        ).status_code
        == 401
    )
    assert client.get("/api/v1/auth/me", headers=_auth(other)).status_code == 200
    fresh = _login(client, "out@example.com").json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=_auth(fresh)).status_code == 200


def test_token_without_version_claim_still_valid(client: TestClient):
    """Токены, выданные до появления ``ver``, живут до своего exp, пока версия не менялась."""
    from datetime import UTC, datetime, timedelta

    import jwt

    user_id = _register(client, "legacy-token@example.com").json()["user"]["id"]
    now = datetime.now(UTC)
    legacy = jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=5)}, "test-secret", algorithm="HS256"
    )
    assert client.get("/api/v1/auth/me", headers=_auth(legacy)).status_code == 200


def test_deactivated_user_is_rejected_on_next_request(client: TestClient):
    """Текущий пользователь читается из базы на каждый запрос — без кэша."""
    from sqlalchemy import update

    from backend.db.models import UserRow
    from backend.db.session import session_scope

    token = _register(client, "blocked@example.com").json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 200
    with session_scope() as s:
        s.execute(update(UserRow).where(UserRow.email == "blocked@example.com").values(is_active=False))
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 401
