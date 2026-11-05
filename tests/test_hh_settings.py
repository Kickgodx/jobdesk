"""Настройки HH-сервиса: бэкенд sync, сервисные токены в prod (platform/runtime-config, internal-services)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobdesk_hh.settings import HhSettings
from jobdesk_platform.service_tokens import DEV_CORE_INTERNAL_TOKEN, DEV_HH_SERVICE_TOKEN

_ENV_KEYS = (
    "APP_ENV",
    "HH_SYNC_JOB_BACKEND",
    "HH_SERVICE_TOKEN",
    "CORE_INTERNAL_TOKEN",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
)
_TOKEN = "t" * 32


@pytest.fixture
def make_settings(monkeypatch):
    """HhSettings только из переданных переменных: без .env разработчика и окружения."""

    def _make(**env: str) -> HhSettings:
        for key in _ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return HhSettings(_env_file=None)

    return _make


def test_job_backend_typo_names_variable(make_settings):
    with pytest.raises(ValidationError) as exc:
        make_settings(HH_SYNC_JOB_BACKEND="redis")
    assert "HH_SYNC_JOB_BACKEND" in str(exc.value)


@pytest.mark.parametrize(("raw", "expected"), [(" RQ ", "rq"), ("Inprocess", "inprocess"), ("", "inprocess")])
def test_job_backend_ignores_case_and_spaces(make_settings, raw, expected):
    assert make_settings(HH_SYNC_JOB_BACKEND=raw).hh_sync_job_backend_normalized == expected


def test_dev_tokens_default_to_values_shared_with_core(make_settings):
    s = make_settings()
    assert s.hh_service_token == DEV_HH_SERVICE_TOKEN
    assert s.core_internal_token == DEV_CORE_INTERNAL_TOKEN
    assert s.database_url.endswith("@127.0.0.1:5432/tgjobs")


@pytest.mark.parametrize("missing", ["HH_SERVICE_TOKEN", "CORE_INTERNAL_TOKEN"])
def test_prod_requires_service_tokens(make_settings, missing):
    env = {"APP_ENV": "prod", "HH_SERVICE_TOKEN": _TOKEN, "CORE_INTERNAL_TOKEN": _TOKEN, missing: "leaked-value"}
    with pytest.raises(ValidationError, match=missing) as exc:
        make_settings(**env)
    assert "leaked-value" not in str(exc.value)


def test_prod_with_tokens_starts(make_settings):
    s = make_settings(APP_ENV="prod", HH_SERVICE_TOKEN=_TOKEN, CORE_INTERNAL_TOKEN=_TOKEN)
    assert s.is_production is True


def test_non_postgres_database_rejected(make_settings):
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        make_settings(DATABASE_URL="sqlite:///hh.db")
