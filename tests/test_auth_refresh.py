"""Refresh-сессии: httpOnly cookie, ротация, grace для параллельных вкладок, отзыв."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from backend.db.models import RefreshTokenRow, UserRow
from backend.db.session import session_scope
from backend.services import auth_service
from backend.services.db_cleanup import purge_service_tables
from jobdesk.config.schema import RetentionConfig

COOKIE = auth_service.REFRESH_COOKIE


@pytest.fixture
def client(api_client: TestClient) -> TestClient:
    api_client.cookies.clear()
    return api_client


def _register(client: TestClient, email: str = "user@example.com", password: str = "secret1") -> dict:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _refresh(client: TestClient, cookie: str | None = None):
    """POST /auth/refresh с cookie из jar клиента или явно заданным (явный Cookie-заголовок
    перекрывает jar; ответ при этом всё равно обновляет jar)."""
    headers = {"Cookie": f"{COOKIE}={cookie}"} if cookie is not None else None
    return client.post("/api/v1/auth/refresh", headers=headers)


def _me(client: TestClient, token: str) -> int:
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code


def _age_rotation(seconds: float) -> None:
    """Сдвинуть момент ротации в прошлое — дальше grace-окна."""
    with session_scope() as session:
        session.execute(
            update(RefreshTokenRow)
            .where(RefreshTokenRow.rotated_at.is_not(None))
            .values(rotated_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )


def test_login_sets_httponly_strict_cookie_on_auth_path(client: TestClient):
    _register(client)
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": "user@example.com", "password": "secret1"})
    assert r.status_code == 200
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/api/v1/auth" in set_cookie
    assert "Secure" not in set_cookie  # dev: http://localhost
    # Значение cookie в БД не лежит — только хэш.
    with session_scope() as session:
        hashes = list(session.scalars(select(RefreshTokenRow.token_hash)))
    assert client.cookies.get(COOKIE) not in hashes


def test_refresh_rotates_cookie_and_issues_working_access_token(client: TestClient):
    _register(client)
    first = client.cookies.get(COOKIE)

    r = _refresh(client)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "user@example.com"
    assert _me(client, body["access_token"]) == 200
    second = client.cookies.get(COOKIE)
    assert second and second != first

    # Новый токен тоже обменивается.
    assert _refresh(client).status_code == 200


def test_refresh_without_or_with_unknown_cookie_is_401(client: TestClient):
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "NOT_AUTHENTICATED"
    assert _refresh(client, "garbage").status_code == 401


def test_parallel_refresh_within_grace_keeps_session(client: TestClient):
    """Две вкладки с одним cookie: проигравшая гонку получает access без отзыва сессии."""
    _register(client)
    old = client.cookies.get(COOKIE)
    assert _refresh(client).status_code == 200
    new = client.cookies.get(COOKIE)

    r = _refresh(client, old)
    assert r.status_code == 200
    assert "set-cookie" not in r.headers  # успевшая вкладка уже положила новый cookie
    assert _me(client, r.json()["access_token"]) == 200
    assert _refresh(client, new).status_code == 200


def test_reuse_after_grace_revokes_whole_session(client: TestClient):
    _register(client)
    stolen = client.cookies.get(COOKIE)
    assert _refresh(client).status_code == 200
    legit = client.cookies.get(COOKIE)
    _age_rotation(auth_service.REFRESH_REUSE_GRACE.total_seconds() + 5)

    assert _refresh(client, stolen).status_code == 401
    # Вместе с украденным отозвана вся сессия, включая актуальный токен.
    assert _refresh(client, legit).status_code == 401


def test_reuse_does_not_touch_other_sessions(client: TestClient):
    _register(client)
    other_session = client.cookies.get(COOKIE)
    client.cookies.clear()
    client.post("/api/v1/auth/login", json={"email": "user@example.com", "password": "secret1"})
    stolen = client.cookies.get(COOKIE)
    assert _refresh(client).status_code == 200
    _age_rotation(3600)

    assert _refresh(client, stolen).status_code == 401
    assert _refresh(client, other_session).status_code == 200


def test_expired_refresh_token_is_401(client: TestClient):
    _register(client)
    with session_scope() as session:
        session.execute(update(RefreshTokenRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert _refresh(client).status_code == 401


def test_logout_revokes_session_and_clears_cookie(client: TestClient):
    _register(client)
    cookie = client.cookies.get(COOKIE)

    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert f'{COOKIE}=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert _refresh(client, cookie).status_code == 401


def test_logout_without_cookie_is_ok(client: TestClient):
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_logout_all_and_password_change_revoke_refresh(client: TestClient):
    token = _register(client)["access_token"]
    before = client.cookies.get(COOKIE)

    r = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "secret1", "new_password": "secret2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    after = client.cookies.get(COOKIE)
    assert after and after != before  # эта сессия получила новую refresh-сессию
    assert _refresh(client, before).status_code == 401
    assert _refresh(client, after).status_code == 200

    current = client.cookies.get(COOKIE)
    r = client.post("/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert r.status_code == 204
    assert _refresh(client, current).status_code == 401


def test_disabled_user_cannot_refresh(client: TestClient):
    _register(client)
    with session_scope() as session:
        session.execute(update(UserRow).values(is_active=False))
    assert _refresh(client).status_code == 401


def test_cleanup_purges_expired_and_revoked_keeps_live(client: TestClient):
    _register(client)
    assert _refresh(client).status_code == 200  # 1 ротированный (живой) + 1 актуальный
    with session_scope() as session:
        user_id = session.scalar(select(UserRow.id))
        now = datetime.now(UTC)
        session.add_all(
            [
                RefreshTokenRow(
                    user_id=user_id,
                    token_hash="a" * 64,
                    family_id="f1",
                    token_version=0,
                    expires_at=now - timedelta(days=1),
                ),
                RefreshTokenRow(
                    user_id=user_id,
                    token_hash="b" * 64,
                    family_id="f2",
                    token_version=0,
                    expires_at=now + timedelta(days=1),
                    revoked_at=now,
                ),
            ]
        )

    stats = purge_service_tables(RetentionConfig())

    assert stats.refresh_tokens == 2
    with session_scope() as session:
        assert len(list(session.scalars(select(RefreshTokenRow.id)))) == 2
    assert _refresh(client).status_code == 200


def test_cookie_secure_in_prod_settings(monkeypatch):
    from backend.settings import Settings

    monkeypatch.delenv("REFRESH_COOKIE_SECURE", raising=False)
    prod = {"APP_ENV": "prod", "JWT_SECRET": "x" * 32, "CORE_INTERNAL_TOKEN": "t" * 32}
    assert Settings(**prod).refresh_cookie_secure is True
    assert Settings(APP_ENV="dev").refresh_cookie_secure is False
    assert Settings(**prod, REFRESH_COOKIE_SECURE=False).refresh_cookie_secure is False
