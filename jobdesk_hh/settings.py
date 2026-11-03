"""Настройки процесса HH-сервиса (env + ``.env``); доменные — секции ``hh`` и ``retention`` в config.yaml.

``APP_ENV`` (dev | prod) меняет только умолчания (``_apply_app_env``), как у ядра: в prod
обязательны сервисные токены, в dev они получают общие с ядром dev-значения.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobdesk_platform.db import default_database_url, require_postgres_url
from jobdesk_platform.service_tokens import DEV_CORE_INTERNAL_TOKEN, DEV_HH_SERVICE_TOKEN, resolve_service_token

JobBackend = Literal["inprocess", "rq"]
_APP_ENV_ALIASES = {"": "dev", "development": "dev", "local": "dev", "production": "prod"}


class HhSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Ошибка валидации печатается при старте — без этого в лог попадут значения env (секреты).
        hide_input_in_errors=True,
    )

    app_env: Literal["dev", "prod"] = Field(default="dev", alias="APP_ENV")
    # Пусто → Postgres из compose с хоста (как у ядра): та же база, схема hh.
    database_url: str = Field(default="", alias="DATABASE_URL")
    postgres_password: str | None = Field(default=None, alias="POSTGRES_PASSWORD")
    config_path: Path = Field(default=Path("config.yaml"), alias="CONFIG_PATH")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    # HH.ru: готовый Bearer-токен (без client_credentials). Email — для User-Agent.
    hh_api_token: str | None = Field(default=None, alias="HH_API_TOKEN")
    hh_contact_email: str | None = Field(default=None, alias="HH_CONTACT_EMAIL")

    # HTTP API сервиса (вызывает только ядро — с HH_SERVICE_TOKEN).
    hh_service_host: str = Field(default="127.0.0.1", alias="HH_SERVICE_HOST")
    hh_service_port: int = Field(default=8100, alias="HH_SERVICE_PORT")
    # /metrics процесса API сервиса — отдельный порт, только в Docker-сети. 0 — не поднимать.
    hh_service_metrics_port: int = Field(default=9105, ge=0, alias="HH_SERVICE_METRICS_PORT")
    hh_service_token: str | None = Field(default=None, alias="HH_SERVICE_TOKEN")

    # Внутренний API ядра: вакансии читаются и пишутся только через него.
    core_internal_url: str = Field(default="http://127.0.0.1:8000", alias="CORE_INTERNAL_URL")
    core_internal_token: str | None = Field(default=None, alias="CORE_INTERNAL_TOKEN")
    core_timeout_seconds: float = Field(default=30.0, gt=0, alias="CORE_TIMEOUT_SECONDS")

    # HH sync jobs: inprocess (поток в процессе сервиса) или rq (Redis Queue + hh-worker).
    hh_sync_job_backend: JobBackend = Field(default="inprocess", alias="HH_SYNC_JOB_BACKEND")
    hh_worker_metrics_port: int = Field(default=9103, alias="HH_WORKER_METRICS_PORT")
    hh_sync_rq_job_timeout_seconds: int = Field(default=3600, alias="HH_SYNC_RQ_JOB_TIMEOUT_SECONDS")
    hh_sync_job_lease_seconds: int = Field(default=120, alias="HH_SYNC_JOB_LEASE_SECONDS")
    hh_sync_job_stuck_minutes: int = Field(default=15, alias="HH_SYNC_JOB_STUCK_MINUTES")
    # Sync безопасно повторять (detail уже скачанных listings не перекачивается).
    hh_sync_job_max_attempts: int = Field(default=2, ge=1, alias="HH_SYNC_JOB_MAX_ATTEMPTS")
    job_recover_interval_seconds: int = Field(default=60, alias="JOB_RECOVER_INTERVAL_SECONDS")
    # Очистка кэша выдачи и старых sync: при старте и затем с этим интервалом (не чаще раза в минуту). 0 — выкл.
    hh_maintenance_interval_seconds: int = Field(default=3600, ge=0, alias="HH_MAINTENANCE_INTERVAL_SECONDS")
    # RQ-воркер, чей heartbeat молчит дольше, завершает процесс (рестарт по restart policy). 0 — выкл.
    worker_stall_exit_seconds: int = Field(default=900, ge=0, alias="WORKER_STALL_EXIT_SECONDS")

    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_traces_sample_ratio: float = Field(default=1.0, alias="OTEL_TRACES_SAMPLE_RATIO")

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        return _APP_ENV_ALIASES.get(normalized, normalized)

    @field_validator("hh_sync_job_backend", mode="before")
    @classmethod
    def _normalize_job_backend(cls, value: Any) -> Any:
        # Другое значение отклоняет Literal: опечатка не должна молча включать inprocess.
        if value is None:
            return "inprocess"
        if not isinstance(value, str):
            return value
        return value.strip().lower() or "inprocess"

    @model_validator(mode="after")
    def _apply_app_env(self) -> Self:
        if not self.database_url.strip():
            self.database_url = default_database_url(self.postgres_password)
        require_postgres_url(self.database_url)
        self.hh_service_token = resolve_service_token(
            self.hh_service_token,
            env_name="HH_SERVICE_TOKEN",
            is_production=self.is_production,
            dev_default=DEV_HH_SERVICE_TOKEN,
        )
        self.core_internal_token = resolve_service_token(
            self.core_internal_token,
            env_name="CORE_INTERNAL_TOKEN",
            is_production=self.is_production,
            dev_default=DEV_CORE_INTERNAL_TOKEN,
        )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    @property
    def hh_sync_job_backend_normalized(self) -> JobBackend:
        return self.hh_sync_job_backend


@lru_cache
def get_hh_settings() -> HhSettings:
    return HhSettings()
