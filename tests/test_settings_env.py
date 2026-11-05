"""APP_ENV: dev-умолчания удобны локально, prod — закрытые; явный env побеждает в обоих."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.settings import Settings, check_api_workers, check_exposure

_PROD_SECRET = "x" * 32
_PROD_TOKEN = "t" * 32
_ENV_KEYS = (
    "APP_ENV",
    "JWT_SECRET",
    "CORE_INTERNAL_TOKEN",
    "HH_SERVICE_URL",
    "HH_SERVICE_TOKEN",
    "AUTH_REGISTRATION_OPEN",
    "HTTP_LOG_BODIES",
    "API_HOST",
    "API_PUBLIC_ADDR",
    "ALLOW_PUBLIC_DEV",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "PARSE_JOB_BACKEND",
    "LLM_JOB_BACKEND",
    "MAINTENANCE_BACKEND",
    "API_WORKERS",
    "REDIS_URL",
    "DB_POOL_TIMEOUT",
    "API_QUEUE_TIMEOUT_SECONDS",
)


@pytest.fixture
def make_settings(monkeypatch):
    """Settings только из переданных переменных: без .env разработчика и окружения."""

    def _make(**env: str) -> Settings:
        for key in _ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        # Токен ядра обязателен в prod; тесты про него передают значение явно.
        env.setdefault("CORE_INTERNAL_TOKEN", _PROD_TOKEN)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings(_env_file=None)

    return _make


def test_dev_is_default_and_keeps_local_conveniences(make_settings):
    s = make_settings()
    assert s.app_env == "dev"
    assert s.is_production is False
    assert s.auth_registration_open is True
    assert s.http_log_bodies is True
    assert s.jwt_secret is None  # эфемерный секрет — только в dev


def test_prod_closes_defaults(make_settings):
    s = make_settings(APP_ENV="prod", JWT_SECRET=_PROD_SECRET)
    assert s.is_production is True
    assert s.auth_registration_open is False
    assert s.http_log_bodies is False


@pytest.mark.parametrize("secret", [None, "", "   ", "short-secret", "x" * 31])
def test_prod_requires_long_jwt_secret(make_settings, secret):
    env = {"APP_ENV": "prod"}
    if secret is not None:
        env["JWT_SECRET"] = secret
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make_settings(**env)


def test_startup_error_does_not_echo_env_values(make_settings):
    with pytest.raises(ValidationError) as exc:
        make_settings(APP_ENV="prod", JWT_SECRET="short-secret")
    assert "short-secret" not in str(exc.value)


def test_explicit_values_win_in_prod(make_settings):
    s = make_settings(
        APP_ENV="prod",
        JWT_SECRET=_PROD_SECRET,
        AUTH_REGISTRATION_OPEN="true",
        HTTP_LOG_BODIES="true",
    )
    assert s.auth_registration_open is True
    assert s.http_log_bodies is True


def test_explicit_values_win_in_dev(make_settings):
    s = make_settings(AUTH_REGISTRATION_OPEN="false", HTTP_LOG_BODIES="false")
    assert s.auth_registration_open is False
    assert s.http_log_bodies is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "dev"),
        ("DEV", "dev"),
        ("development", "dev"),
        ("local", "dev"),
        (" Prod ", "prod"),
        ("production", "prod"),
    ],
)
def test_app_env_aliases(make_settings, raw, expected):
    assert make_settings(APP_ENV=raw, JWT_SECRET=_PROD_SECRET).app_env == expected


def test_unknown_app_env_is_rejected(make_settings):
    # Опечатка не должна молча включать dev-умолчания.
    with pytest.raises(ValidationError):
        make_settings(APP_ENV="staging", JWT_SECRET=_PROD_SECRET)


def test_unknown_job_backend_is_rejected(make_settings):
    # Опечатка не должна молча переводить задачи в процесс API.
    with pytest.raises(ValidationError) as exc:
        make_settings(LLM_JOB_BACKEND="redis-queue")
    assert "LLM_JOB_BACKEND" in str(exc.value)
    assert "redis-queue" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" RQ ", "rq"), ("Inprocess", "inprocess"), ("", "inprocess"), ("  ", "inprocess")],
)
def test_job_backend_ignores_case_and_spaces(make_settings, raw, expected):
    s = make_settings(PARSE_JOB_BACKEND=raw, LLM_JOB_BACKEND=raw)
    assert s.parse_job_backend_normalized == expected
    assert s.llm_job_backend_normalized == expected


def test_job_backend_defaults_to_inprocess(make_settings):
    s = make_settings()
    assert (s.parse_job_backend, s.llm_job_backend) == ("inprocess",) * 2


def test_dev_on_loopback_starts(make_settings):
    assert check_exposure(make_settings()) is None  # API_HOST по умолчанию 127.0.0.1
    assert check_exposure(make_settings(API_HOST="0.0.0.0", API_PUBLIC_ADDR="127.0.0.1")) is None
    assert check_exposure(make_settings(API_HOST="localhost")) is None
    assert check_exposure(make_settings(API_HOST="::1")) is None


@pytest.mark.parametrize(
    "env",
    [
        {"API_HOST": "0.0.0.0"},
        {"API_HOST": "::"},
        {"API_HOST": "192.168.1.10"},
        {"API_HOST": "0.0.0.0", "API_PUBLIC_ADDR": "0.0.0.0"},  # compose с BIND_ADDR=0.0.0.0
        {"API_PUBLIC_ADDR": "my-host.example"},
    ],
)
def test_dev_on_public_addr_refuses_to_start(make_settings, env):
    with pytest.raises(RuntimeError, match="APP_ENV=prod"):
        check_exposure(make_settings(**env))


def test_dev_on_public_addr_allowed_explicitly(make_settings):
    warning = check_exposure(make_settings(API_HOST="0.0.0.0", ALLOW_PUBLIC_DEV="true"))
    assert warning is not None and "ALLOW_PUBLIC_DEV" in warning


def test_prod_on_public_addr_starts(make_settings):
    assert check_exposure(make_settings(APP_ENV="prod", JWT_SECRET=_PROD_SECRET, API_HOST="0.0.0.0")) is None


def test_database_url_defaults_to_compose_postgres_password(make_settings):
    assert make_settings().database_url == "postgresql+psycopg://tgjobs:tgjobs@127.0.0.1:5432/tgjobs"
    s = make_settings(POSTGRES_PASSWORD="p@ss/word")
    assert s.database_url == "postgresql+psycopg://tgjobs:p%40ss%2Fword@127.0.0.1:5432/tgjobs"
    explicit = "postgresql+psycopg://u:x@db:5432/other"
    assert make_settings(POSTGRES_PASSWORD="ignored", DATABASE_URL=explicit).database_url == explicit


def test_database_url_must_be_postgres(make_settings):
    with pytest.raises(ValueError, match="только PostgreSQL"):
        make_settings(DATABASE_URL="sqlite:///tgjobs.db")


_MULTI_OK = {
    "API_WORKERS": "4",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": _PROD_SECRET,
    "PARSE_JOB_BACKEND": "rq",
    "LLM_JOB_BACKEND": "rq",
}


def test_api_workers_default_is_one(make_settings):
    s = make_settings()
    assert s.api_workers == 1
    check_api_workers(s)  # один процесс — без требований


@pytest.mark.parametrize("raw", ["0", "-1", "four"])
def test_api_workers_invalid_value_names_variable(make_settings, raw):
    with pytest.raises(ValidationError) as exc:
        make_settings(API_WORKERS=raw)
    assert "API_WORKERS" in str(exc.value)


def test_several_api_workers_with_shared_state_start(make_settings):
    check_api_workers(make_settings(**_MULTI_OK))


@pytest.mark.parametrize(
    ("drop", "named"),
    [
        ({"REDIS_URL": ""}, "REDIS_URL"),
        ({"JWT_SECRET": ""}, "JWT_SECRET"),
        ({"LLM_JOB_BACKEND": "inprocess"}, "LLM_JOB_BACKEND"),
        ({"PARSE_JOB_BACKEND": "inprocess"}, "PARSE_JOB_BACKEND"),
    ],
)
def test_several_api_workers_require_shared_state(make_settings, drop, named):
    with pytest.raises(RuntimeError, match=named):
        check_api_workers(make_settings(**{**_MULTI_OK, **drop}))


def test_rq_worker_settings_ignore_api_workers(make_settings):
    """Воркер RQ читает тот же .env: API_WORKERS=4 с его inprocess-бэкендами не мешает старту."""
    s = make_settings(API_WORKERS="4", LLM_JOB_BACKEND="rq")
    assert s.api_workers == 4


def test_queue_and_pool_timeouts_default(make_settings):
    s = make_settings()
    assert s.api_queue_timeout_seconds == 2.0
    assert s.db_pool_timeout is None  # умолчание по роли процесса (backend/db/session.py)
    s = make_settings(API_QUEUE_TIMEOUT_SECONDS="0", DB_POOL_TIMEOUT="1.5")
    assert (s.api_queue_timeout_seconds, s.db_pool_timeout) == (0.0, 1.5)


@pytest.mark.parametrize(
    ("name", "raw"),
    [("DB_POOL_TIMEOUT", "0"), ("DB_POOL_TIMEOUT", "-1"), ("API_QUEUE_TIMEOUT_SECONDS", "-1")],
)
def test_invalid_timeouts_name_variable(make_settings, name, raw):
    with pytest.raises(ValidationError) as exc:
        make_settings(**{name: raw})
    assert name in str(exc.value)


@pytest.mark.parametrize(("raw", "expected"), [(" Worker ", "worker"), ("", "inprocess"), (None, "inprocess")])
def test_maintenance_backend_ignores_case_and_spaces(make_settings, raw, expected):
    s = make_settings() if raw is None else make_settings(MAINTENANCE_BACKEND=raw)
    assert s.maintenance_backend == expected


def test_unknown_maintenance_backend_is_rejected(make_settings):
    with pytest.raises(ValidationError) as exc:
        make_settings(MAINTENANCE_BACKEND="rq")
    assert "MAINTENANCE_BACKEND" in str(exc.value)


# --- сервисные токены (platform/internal-services) -------------------------------------


@pytest.mark.parametrize("token", ["", "short-token"])
def test_prod_requires_core_internal_token(make_settings, token):
    with pytest.raises(ValidationError, match="CORE_INTERNAL_TOKEN") as exc:
        make_settings(APP_ENV="prod", JWT_SECRET=_PROD_SECRET, CORE_INTERNAL_TOKEN=token)
    if token:
        assert token not in str(exc.value)


def test_prod_requires_hh_service_token_only_with_hh_url(make_settings):
    assert make_settings(APP_ENV="prod", JWT_SECRET=_PROD_SECRET).hh_service_token is None
    with pytest.raises(ValidationError, match="HH_SERVICE_TOKEN"):
        make_settings(APP_ENV="prod", JWT_SECRET=_PROD_SECRET, HH_SERVICE_URL="http://hh-service:8100")
    s = make_settings(
        APP_ENV="prod", JWT_SECRET=_PROD_SECRET, HH_SERVICE_URL="http://hh-service:8100", HH_SERVICE_TOKEN=_PROD_TOKEN
    )
    assert s.hh_service_token == _PROD_TOKEN


def test_dev_service_tokens_default_to_shared_dev_values(make_settings):
    from jobdesk_platform.service_tokens import DEV_CORE_INTERNAL_TOKEN, DEV_HH_SERVICE_TOKEN

    s = make_settings(CORE_INTERNAL_TOKEN="")
    assert s.core_internal_token == DEV_CORE_INTERNAL_TOKEN
    assert s.hh_service_token == DEV_HH_SERVICE_TOKEN
    assert s.hh_service_url is None
