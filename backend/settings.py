"""Настройки backend API (env + defaults)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobdesk_platform.db import default_database_url, require_postgres_url
from jobdesk_platform.service_tokens import DEV_CORE_INTERNAL_TOKEN, DEV_HH_SERVICE_TOKEN, resolve_service_token

_APP_ENV_ALIASES = {"": "dev", "development": "dev", "local": "dev", "production": "prod"}
_MIN_JWT_SECRET_LENGTH = 32
JobBackend = Literal["inprocess", "rq"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Ошибка валидации печатается при старте — без этого в лог попадут значения env (секреты).
        hide_input_in_errors=True,
    )

    # dev — умолчания, удобные для локальной разработки; prod — закрытые
    # (см. _apply_app_env). Явно заданная переменная побеждает в обоих режимах.
    app_env: Literal["dev", "prod"] = Field(default="dev", alias="APP_ENV")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    # Процессов API на одном порту (jobdesk-api / backend.main:main). Больше 1 — только с
    # общим состоянием в Redis и задачами в rq (см. check_api_workers). `uvicorn --reload`
    # всегда один процесс.
    api_workers: int = Field(default=1, ge=1, alias="API_WORKERS")
    # Адрес, на котором API доступен снаружи процесса. В compose процесс слушает 0.0.0.0
    # внутри контейнера, а наружу порт публикуется на BIND_ADDR — его и передаём сюда.
    # Пусто → API_HOST. dev на не-loopback адресе backend не запускает (см. check_exposure).
    api_public_addr: str | None = Field(default=None, alias="API_PUBLIC_ADDR")
    # Осознанно запустить dev на не-loopback адресе (доверенная сеть): вместо отказа — warning.
    allow_public_dev: bool = Field(default=False, alias="ALLOW_PUBLIC_DEV")
    # /metrics backend'а — на отдельном порту (как у воркеров), а не на порту API:
    # в compose он не публикуется на хост, Prometheus ходит по Docker-сети. 0 — не поднимать.
    api_metrics_port: int = Field(default=9100, ge=0, alias="API_METRICS_PORT")
    # Кэш COUNT-агрегатов db_* для /metrics (сек): частые scrape не нагружают Postgres; 0 — без кэша.
    metrics_db_stats_cache_seconds: float = Field(default=30.0, ge=0, alias="METRICS_DB_STATS_CACHE_SECONDS")
    # Потолок строк экспорта (xlsx списка и прогона, JSON прогона): сверх — обрезка с пометкой в файле.
    export_max_rows: int = Field(default=50_000, ge=1, alias="EXPORT_MAX_ROWS")
    # Пул SQLAlchemy на процесс. Пусто → по роли процесса (backend/db/session.py: API — под
    # threadpool sync-эндпоинтов, воркеры — 2–4 + overflow): N реплик llm-worker не съедают
    # max_connections Postgres.
    db_pool_size: int | None = Field(default=None, ge=1, alias="DB_POOL_SIZE")
    db_max_overflow: int | None = Field(default=None, ge=0, alias="DB_MAX_OVERFLOW")
    # Сколько секунд ждать свободное соединение пула. Пусто → по роли: API 5 с (с лимитом
    # одновременных запросов ожидание — миллисекунды), воркеры 30 с (LLM-потоки и AI-кэш).
    db_pool_timeout: float | None = Field(default=None, gt=0, alias="DB_POOL_TIMEOUT")
    # Сколько секунд запрос ждёт места обработки в процессе API; не дождался — 503 OVERLOADED
    # (backend/overload.py). 0 — отклонять сразу, если все места заняты.
    api_queue_timeout_seconds: float = Field(default=2.0, ge=0, alias="API_QUEUE_TIMEOUT_SECONDS")
    # Пусто → Postgres из compose с хоста, пароль — POSTGRES_PASSWORD из того же .env
    # (см. _apply_app_env): backend, CLI и alembic с хоста работают без своего DATABASE_URL.
    database_url: str = Field(default="", alias="DATABASE_URL")
    postgres_password: str | None = Field(default=None, alias="POSTGRES_PASSWORD")
    config_path: Path = Field(default=Path("config.yaml"), alias="CONFIG_PATH")
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
    )
    api_key: str | None = Field(default=None, alias="API_KEY")
    jwt_secret: str | None = Field(default=None, alias="JWT_SECRET")
    # Access-токен короткий: сессию держит refresh-cookie, UI продлевает access молча.
    jwt_ttl_minutes: int = Field(default=15, ge=1, alias="JWT_TTL_MINUTES")
    # Срок refresh-сессии; скользящий — каждое обновление access продлевает его заново.
    refresh_ttl_days: int = Field(default=30, ge=1, alias="REFRESH_TTL_DAYS")
    # None → по APP_ENV: prod — true (cookie только по HTTPS), dev — false (http://localhost).
    refresh_cookie_secure: bool | None = Field(default=None, alias="REFRESH_COOKIE_SECURE")
    # Лимиты /auth (0 — выключить): попыток входа/регистрации с одного IP в минуту и
    # неудачных входов на один email за окно — дальше 429 до конца окна, без argon2.
    auth_rate_limit_per_minute: int = Field(default=20, ge=0, alias="AUTH_RATE_LIMIT_PER_MINUTE")
    auth_max_failed_logins: int = Field(default=5, ge=0, alias="AUTH_MAX_FAILED_LOGINS")
    auth_failed_login_window_seconds: int = Field(default=900, ge=1, alias="AUTH_FAILED_LOGIN_WINDOW_SECONDS")
    # None → по APP_ENV: dev — true, prod — false.
    auth_registration_open: bool | None = Field(default=None, alias="AUTH_REGISTRATION_OPEN")
    http_log_bodies: bool | None = Field(default=None, alias="HTTP_LOG_BODIES")
    http_log_body_limit: int = Field(default=4096, alias="HTTP_LOG_BODY_LIMIT")
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_traces_sample_ratio: float = Field(default=1.0, alias="OTEL_TRACES_SAMPLE_RATIO")
    http_log_exclude_paths: str = Field(
        # Агрегаты (vacancies/metrics, analytics/*) — крупные JSON; под polling/ab
        # заливают Loki (default ingestion 4MB/s → 429) и раздувают docker-логи.
        default=(
            "/api/v1/health,/api/v1/vacancies/metrics,"
            "/api/v1/analytics/salaries,/api/v1/analytics/sources,/api/v1/analytics/companies,"
            "/docs,/docs/oauth2-redirect,/redoc,/openapi.json,/favicon.ico"
        ),
        alias="HTTP_LOG_EXCLUDE_PATHS",
    )
    # Внутренний API ядра (/internal/v1): токен, который ядро принимает от сервисов.
    # Пусто → dev-значение (одинаковое у сервисов), в prod — обязателен (см. _apply_app_env).
    core_internal_token: str | None = Field(default=None, alias="CORE_INTERNAL_TOKEN")
    # HH-сервис (jobdesk_hh): адрес, токен, которым ядро его вызывает, и тайм-аут запроса.
    # Пустой адрес — HH не подключён: /api/v1/hh/* отвечают 503 UPSTREAM_UNAVAILABLE.
    hh_service_url: str | None = Field(default=None, alias="HH_SERVICE_URL")
    hh_service_token: str | None = Field(default=None, alias="HH_SERVICE_TOKEN")
    hh_service_timeout_seconds: float = Field(default=10.0, gt=0, alias="HH_SERVICE_TIMEOUT_SECONDS")

    # LLM jobs: inprocess (ThreadPool в API) или rq (Redis Queue + llm-worker).
    llm_job_backend: JobBackend = Field(default="inprocess", alias="LLM_JOB_BACKEND")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    llm_worker_metrics_port: int = Field(default=9101, alias="LLM_WORKER_METRICS_PORT")
    llm_job_stuck_minutes: int = Field(default=15, alias="LLM_JOB_STUCK_MINUTES")
    llm_rq_job_timeout_seconds: int = Field(default=600, alias="LLM_RQ_JOB_TIMEOUT_SECONDS")

    # Parse jobs: inprocess (ThreadPool в API) или rq (Redis Queue + parse-worker).
    parse_job_backend: JobBackend = Field(default="inprocess", alias="PARSE_JOB_BACKEND")
    parse_worker_metrics_port: int = Field(default=9102, alias="PARSE_WORKER_METRICS_PORT")
    parse_job_stuck_minutes: int = Field(default=30, alias="PARSE_JOB_STUCK_MINUTES")
    parse_rq_job_timeout_seconds: int = Field(default=43200, alias="PARSE_RQ_JOB_TIMEOUT_SECONDS")
    parse_job_lease_seconds: int = Field(default=120, alias="PARSE_JOB_LEASE_SECONDS")
    llm_job_lease_seconds: int = Field(default=120, alias="LLM_JOB_LEASE_SECONDS")
    job_recover_interval_seconds: int = Field(default=60, alias="JOB_RECOVER_INTERVAL_SECONDS")
    # Суточная (UTC) квота вызовов LLM на не-админа: LLM-джобы и ручной импорт; 0 — без квоты.
    llm_user_daily_calls: int = Field(default=100, ge=0, alias="LLM_USER_DAILY_CALLS")
    # Кэш агрегатов дашборда и аналитики в памяти API (сек); 0 — без кэша.
    analytics_cache_seconds: float = Field(default=30.0, ge=0, alias="ANALYTICS_CACHE_SECONDS")
    # Сколько раз джобу берут в работу, прежде чем recover завершит её ошибкой
    # вместо очередного requeue (воркер стабильно падает на ней, напр. OOM).
    parse_job_max_attempts: int = Field(default=3, ge=1, alias="PARSE_JOB_MAX_ATTEMPTS")
    llm_job_max_attempts: int = Field(default=2, ge=1, alias="LLM_JOB_MAX_ATTEMPTS")

    # Фоновое обслуживание каталога (индексация эмбеддингов, оценки зарплат, обзор и карта
    # рынка; backend/maintenance.py): inprocess — потоки лидера API (локальный запуск, тесты),
    # worker — отдельный процесс jobdesk-maintenance-worker, API этим не занимается.
    maintenance_backend: Literal["inprocess", "worker"] = Field(default="inprocess", alias="MAINTENANCE_BACKEND")
    maintenance_worker_metrics_port: int = Field(default=9104, alias="MAINTENANCE_WORKER_METRICS_PORT")

    # RQ-воркер, чей heartbeat (цикл RQ или lease джобы) молчит дольше — завершает процесс,
    # чтобы restart policy его перезапустила (Docker unhealthy-контейнер сам не рестартит).
    # 0 — watchdog выключен. Порог healthcheck — WORKER_HEARTBEAT_MAX_AGE_SECONDS (180).
    worker_stall_exit_seconds: int = Field(default=900, ge=0, alias="WORKER_STALL_EXIT_SECONDS")

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        return _APP_ENV_ALIASES.get(normalized, normalized)

    @field_validator("llm_job_backend", "parse_job_backend", "maintenance_backend", mode="before")
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
        if self.auth_registration_open is None:
            self.auth_registration_open = not self.is_production
        if self.http_log_bodies is None:
            self.http_log_bodies = not self.is_production
        if self.refresh_cookie_secure is None:
            self.refresh_cookie_secure = self.is_production
        if self.is_production and len((self.jwt_secret or "").strip()) < _MIN_JWT_SECRET_LENGTH:
            # Эфемерный секрет допустим только в dev: в prod он сбрасывает сессии при
            # каждом рестарте и расходится между процессами.
            raise ValueError(f"APP_ENV=prod: задайте JWT_SECRET не короче {_MIN_JWT_SECRET_LENGTH} символов")
        self.core_internal_token = resolve_service_token(
            self.core_internal_token,
            env_name="CORE_INTERNAL_TOKEN",
            is_production=self.is_production,
            dev_default=DEV_CORE_INTERNAL_TOKEN,
        )
        if (self.hh_service_url or "").strip() or not self.is_production:
            # Токен HH нужен, только если ядро ходит в HH-сервис.
            self.hh_service_token = resolve_service_token(
                self.hh_service_token,
                env_name="HH_SERVICE_TOKEN",
                is_production=self.is_production,
                dev_default=DEV_HH_SERVICE_TOKEN,
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    @property
    def api_exposed_addr(self) -> str:
        return (self.api_public_addr or "").strip() or self.api_host.strip()

    @property
    def api_is_publicly_exposed(self) -> bool:
        return not _is_loopback(self.api_exposed_addr)

    @property
    def llm_job_backend_normalized(self) -> JobBackend:
        return self.llm_job_backend

    @property
    def parse_job_backend_normalized(self) -> JobBackend:
        return self.parse_job_backend

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def http_log_exclude_path_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.http_log_exclude_paths.split(",") if p.strip())


def _is_loopback(addr: str) -> bool:
    """127.0.0.0/8, ::1 и localhost. Имя хоста, 0.0.0.0 и :: — не loopback."""
    host = addr.strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_exposure(settings: Settings) -> str | None:
    """Отказ стартовать API в dev, если он доступен не только с этой машины.

    dev-умолчания (открытая регистрация, тела запросов с резюме в логах,
    эфемерный JWT-секрет) безопасны только на loopback. Возвращает текст
    предупреждения, если это осознанно разрешено ``ALLOW_PUBLIC_DEV``.
    """
    if settings.is_production or not settings.api_is_publicly_exposed:
        return None
    message = (
        f"APP_ENV=dev, а API доступен на {settings.api_exposed_addr}: регистрация открыта, "
        "тела запросов (резюме) пишутся в лог. Задайте APP_ENV=prod (и JWT_SECRET) "
        "или публикуйте порт на 127.0.0.1"
    )
    if settings.allow_public_dev:
        return f"{message} (разрешено ALLOW_PUBLIC_DEV=true)"
    raise RuntimeError(f"{message}; осознанно разрешить — ALLOW_PUBLIC_DEV=true")


def check_api_workers(settings: Settings) -> None:
    """Отказ стартовать API с ``API_WORKERS`` > 1 без общего для процессов состояния.

    Не в валидаторе ``Settings``: воркеры RQ читают тот же ``.env`` и не должны
    падать из-за настройки процесса API. Без Redis лимиты входа и одноразовость
    SSE-тикетов живут в памяти процесса, без ``JWT_SECRET`` у каждого процесса свой
    эфемерный секрет (токен одного не примет другой), а задачи ``inprocess``
    выполнялись бы в потоках того процесса, что принял запрос.
    """
    if settings.api_workers <= 1:
        return
    missing: list[str] = []
    if not (settings.redis_url or "").strip():
        missing.append("REDIS_URL")
    if not (settings.jwt_secret or "").strip():
        missing.append("JWT_SECRET")
    for name, backend in (
        ("PARSE_JOB_BACKEND", settings.parse_job_backend),
        ("LLM_JOB_BACKEND", settings.llm_job_backend),
    ):
        if backend != "rq":
            missing.append(f"{name}=rq")
    if missing:
        raise RuntimeError(f"API_WORKERS={settings.api_workers}: несколько процессов API требуют " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]
