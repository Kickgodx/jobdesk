"""Общие фикстуры тестов: конфиг без сети и без AI, Postgres для тестов с БД."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

# --- Postgres -------------------------------------------------------------------------
# База для тестов — тот же Postgres 16, что в docker-compose.yml. Источник:
#   * TEST_DATABASE_URL — готовый сервер (compose-Postgres, CI-сервис); нужна роль с CREATEDB;
#   * иначе testcontainers поднимает образ docker/postgres (postgres:16-alpine + pgvector) в Docker
#     на время прогона; нет образа — собирает его (один раз, дальше из кэша Docker).
# Контейнер поднимает один процесс-контроллер (pytest_configure), xdist-воркеры получают URL
# через env. Каждый воркер создаёт свою базу и прогоняет в ней миграции; перед тестом — TRUNCATE.

_PG_DOCKERFILE_DIR = Path(__file__).resolve().parents[1] / "docker" / "postgres"


def _pg_image() -> str:
    """Тег образа как в docker-compose.yml: версия pgvector — из ARG Dockerfile."""
    text = (_PG_DOCKERFILE_DIR / "Dockerfile").read_text(encoding="utf-8")
    version = re.search(r"^ARG PGVECTOR_VERSION=(\S+)", text, re.MULTILINE).group(1)
    return f"jobdesk-postgres:16-pgvector{version}"


def _ensure_pg_image(image: str) -> None:
    import docker

    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.build(path=str(_PG_DOCKERFILE_DIR), tag=image, rm=True)


# Данные в tmpfs без fsync: тестовой базе durability не нужна, а запись заметно быстрее.
_PG_COMMAND = "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=300"
_PG_ERROR_ENV = "JOBDESK_TEST_PG_ERROR"
_RUN_ID_ENV = "JOBDESK_TEST_RUN_ID"
# Нет тестового сервера: адрес никуда не ведёт (а не в dev-базу из .env), отказ — за секунду.
_NO_DB_URL = "postgresql+psycopg://no-test-db@127.0.0.1:1/none?connect_timeout=1"

_container = None


def _live_llm_selected(config) -> bool:
    """Запущены live-тесты LLM (``-m llm``), а не обычный прогон с ``-m "not llm"``."""
    markexpr = (config.option.markexpr or "").replace(" ", "")
    return "llm" in markexpr and "notllm" not in markexpr


def pytest_configure(config) -> None:
    # backend.main подтягивает .env в окружение: без этого любая LLM-задача, доработавшая после
    # отката подмен теста, ушла бы в настоящий OpenRouter. Пустое значение dotenv не перезапишет.
    if not _live_llm_selected(config):
        os.environ["OPENROUTER_API_KEY"] = ""
    if hasattr(config, "workerinput"):
        return  # xdist-воркер: URL уже в env от контроллера
    os.environ.setdefault(_RUN_ID_ENV, str(os.getpid()))
    if os.environ.get("TEST_DATABASE_URL"):
        return
    global _container
    try:
        from testcontainers.community.postgres import PostgresContainer

        image = _pg_image()
        _ensure_pg_image(image)
        _container = (
            PostgresContainer(image, driver="psycopg")
            .with_command(_PG_COMMAND)
            .with_tmpfs_mount("/var/lib/postgresql/data")
        )
        _container.start()
    except Exception as exc:  # Docker не запущен — тесты без БД всё равно идут
        _container = None
        os.environ[_PG_ERROR_ENV] = f"{type(exc).__name__}: {exc}"
        return
    host = _container.get_container_host_ip()
    # localhost на Windows сначала пробует ::1 и ждёт connect timeout.
    os.environ["TEST_DATABASE_URL"] = _container.get_connection_url(host="127.0.0.1" if host == "localhost" else host)


def pytest_unconfigure(config) -> None:
    global _container
    if _container is not None:
        _container.stop()
        _container = None


def _worker_db_url() -> str | None:
    """База этого xdist-воркера на тестовом сервере (создаёт её ``pg_database``)."""
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        return None
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    name = f"tgjobs_test_{os.environ.get(_RUN_ID_ENV, os.getpid())}_{worker}"
    return sa.make_url(admin_url).set(database=name).render_as_string(hide_password=False)


@dataclass
class PgDatabase:
    url: str
    admin_url: str  # сервер целиком: создание/удаление служебных баз
    engine: sa.Engine  # служебное соединение: TRUNCATE и сброс чужих сессий
    tables: list[str]


@pytest.fixture(scope="session")
def pg_database(default_test_config):
    """Своя база воркера с применёнными миграциями ядра и HH-сервиса (как при их старте)."""
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        reason = os.environ.get(_PG_ERROR_ENV, "TEST_DATABASE_URL не задан")
        pytest.fail(
            f"Нет Postgres для тестов ({reason}). Запустите Docker Desktop "
            "или задайте TEST_DATABASE_URL на сервер с правом CREATEDB.",
            pytrace=False,
        )
    from backend.db.session import init_db, reset_engine
    from backend.settings import get_settings

    url = _worker_db_url()
    name = sa.make_url(url).database
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))

    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    reset_engine()
    init_db()
    reset_engine()
    # Схема HH-сервиса — своя цепочка, как при старте jobdesk_hh.
    from jobdesk_hh.migrate import upgrade_head as hh_upgrade_head

    hh_upgrade_head(url)
    mp.undo()
    get_settings.cache_clear()

    engine = sa.create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=True)
    with engine.connect() as conn:
        # Схемы ядра и сервисов (hh — HH-сервис); имена квалифицированы для TRUNCATE.
        tables = sorted(
            conn.execute(
                sa.text(
                    "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                    "WHERE schemaname IN ('public', 'hh') AND tablename <> 'alembic_version'"
                )
            ).scalars()
        )
    yield PgDatabase(url=url, admin_url=admin_url, engine=engine, tables=tables)
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _clean(db: PgDatabase) -> None:
    with db.engine.begin() as conn:
        # Фоновые потоки прошлого теста (inprocess-джобы, lifespan) могли оставить
        # открытые транзакции — они держали бы TRUNCATE на lock'е.
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(sa.text(f"TRUNCATE {', '.join(db.tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
def pg_db(pg_database, monkeypatch) -> str:
    """Пустая схема (TRUNCATE) и DATABASE_URL на неё; движок приложения пересоздаётся."""
    from backend.db.session import reset_engine
    from backend.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", pg_database.url)
    get_settings.cache_clear()
    reset_engine()
    _clean(pg_database)
    yield pg_database.url
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def pg_empty_db(pg_database, monkeypatch) -> str:
    """Пустая база без миграций (для тестов самих миграций); DATABASE_URL — на неё."""
    from backend.db.session import reset_engine
    from backend.settings import get_settings

    name = f"{sa.make_url(pg_database.url).database}_empty"
    admin = sa.create_engine(pg_database.admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.make_url(pg_database.url).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    reset_engine()
    yield url
    reset_engine()
    get_settings.cache_clear()
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def db_session(pg_db):
    """ORM-сессия на тестовой базе (autoflush, как у ``sessionmaker`` по умолчанию)."""
    from sqlalchemy.orm import sessionmaker

    from backend.db.session import get_engine

    session = sessionmaker(bind=get_engine())()
    yield session
    session.close()


# Минимальный конфиг: fx выключен (upsert/apply и startup-sync не тянут ЦБ в оффлайне).
_TEST_CONFIG = """
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
  embeddings:
    enabled: false
fx:
  enabled: false
digest:
  enabled: false
"""


@pytest.fixture(scope="session", autouse=True)
def default_test_config(tmp_path_factory):
    """CONFIG_PATH по умолчанию для всех тестов; свой monkeypatch.setenv перекрывает."""
    cfg = tmp_path_factory.mktemp("config") / "config.yaml"
    cfg.write_text(_TEST_CONFIG, encoding="utf-8")

    from backend.db.ai_cache import register_db_ai_cache
    from backend.settings import get_settings

    register_db_ai_cache()
    mp = pytest.MonkeyPatch()
    mp.setenv("CONFIG_PATH", str(cfg))
    # Тест без pg_db, задевший БД (сборщик метрик, фоновый поток), попадёт в базу воркера,
    # а не в dev-базу из .env.
    mp.setenv("DATABASE_URL", _worker_db_url() or _NO_DB_URL)
    # Тесты рассчитаны на dev-умолчания и не должны зависеть от APP_ENV в .env разработчика.
    mp.setenv("APP_ENV", "dev")
    # Сервер /metrics на отдельном порту в тестах не поднимаем (TestClient на каждый тест,
    # xdist-процессы делили бы порт) — метрики читаются из REGISTRY (metrics_text).
    mp.setenv("API_METRICS_PORT", "0")
    # Кэш db_* и лимиты /auth — глобальное состояние процесса: между тестами оно протекало
    # бы. Включают их только собственные тесты (test_app_metrics, test_auth).
    mp.setenv("METRICS_DB_STATS_CACHE_SECONDS", "0")
    mp.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "0")
    mp.setenv("AUTH_MAX_FAILED_LOGINS", "0")
    get_settings.cache_clear()
    yield cfg
    mp.undo()
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def no_real_telegram():
    """Тесты не открывают Telethon.

    Прогон в режиме inprocess идёт фоновым потоком; если тест завершился раньше, чем
    поток дошёл до ``run_parse``, его monkeypatch уже снят — и поток пошёл бы в Telegram
    с настоящими ``TELEGRAM_*`` из ``.env`` и сессией из ``sessions/``. Session-scope:
    заглушка переживает отдельный тест; свой ``_channel_parser`` тест ставит поверх.
    """
    import jobdesk.parse_runner as parse_runner

    def _forbidden(config, *, cancellation=None):
        raise RuntimeError("Тест дошёл до настоящего Telethon: подмените run_parse или parse_runner._channel_parser")

    mp = pytest.MonkeyPatch()
    mp.setattr(parse_runner, "_channel_parser", _forbidden)
    yield
    mp.undo()


@pytest.fixture
def otel_exporter():
    """Глобальный TracerProvider с in-memory экспортом; после теста — сброс (как в тестах OTel).

    Модульный ``_TRACER = trace.get_tracer(...)`` навсегда привязан к провайдеру:
    ``ProxyTracer`` — к первому выставленному, а модуль, импортированный при уже
    выставленном, получает SDK-tracer именно этого провайдера. Без сброса span'ы
    второго теста того же модуля уходят в провайдер первого.
    """
    import sys

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.util._once import Once

    from backend.tracing import build_sampler

    def reset() -> None:
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace._TRACER_PROVIDER = None
        for name, module in list(sys.modules.items()):
            if name.startswith(("backend.", "jobdesk.", "jobdesk_hh.", "jobdesk_platform.")) and isinstance(
                getattr(module, "_TRACER", None), trace.Tracer
            ):
                module._TRACER = trace.ProxyTracer(name)

    exp = InMemorySpanExporter()
    provider = TracerProvider(sampler=build_sampler(1.0))
    provider.add_span_processor(SimpleSpanProcessor(exp))
    reset()
    trace.set_tracer_provider(provider)
    yield exp
    provider.shutdown()
    reset()


def metrics_text() -> str:
    """Тело scrape /metrics backend'а (тот же REGISTRY, что отдаёт сервер на API_METRICS_PORT)."""
    from prometheus_client import REGISTRY, generate_latest

    return generate_latest(REGISTRY).decode("utf-8")


def new_parse_run(session):
    """Строка ``parse_runs`` под FK вакансий/связок прогона; возвращает её id."""
    from backend.db.models import ParseRunRow

    row = ParseRunRow(status="completed", config_snapshot={})
    session.add(row)
    session.flush()
    return row.id


def add_legacy_vacancy(session, **values) -> None:
    """Вставить строку ``vacancies`` только с заданными колонками (Core, а не ORM).

    Для тестов миграций: схема поднята до старой ревизии, а ORM-модель вставляет все свои
    колонки, включая добавленные позже (``company_key``, миграция 040).
    """
    from sqlalchemy import insert

    from backend.db.models import VacancyRow

    session.execute(insert(VacancyRow).values(**values))


def new_user(session, email: str = "user@example.com") -> int:
    """Строка ``users`` под FK per-user таблиц; возвращает её id."""
    from backend.db.models import UserRow

    row = UserRow(email=email, password_hash="x")
    session.add(row)
    session.flush()
    return row.id


def login_as_admin(client, email: str = "admin@example.com") -> str:
    """Первый зарегистрированный пользователь — admin; Bearer ставится клиенту по умолчанию."""
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "secret1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["is_admin"] is True
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body["access_token"]


@pytest.fixture
def api_client(monkeypatch, pg_db, default_test_config):
    """TestClient на пустой тестовой базе (схема уже прогнана миграциями)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CONFIG_PATH", str(default_test_config))
    monkeypatch.setenv("JWT_SECRET", "test-secret-value-for-tests")
    monkeypatch.setenv("LLM_JOB_BACKEND", "inprocess")

    from backend.db.session import reset_engine
    from backend.settings import get_settings

    get_settings.cache_clear()

    from backend.main import app
    from backend.queue.rq_conn import reset_redis_connection
    from backend.services.interactive_llm import reset_interactive_client
    from backend.services.llm_job_service import llm_job_runner

    with TestClient(app) as c:
        yield c
    llm_job_runner.reset()
    reset_interactive_client()
    reset_redis_connection()
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def admin_client(api_client):
    """``api_client``, залогиненный первым пользователем (admin)."""
    login_as_admin(api_client)
    return api_client


def clear_settings_caches() -> None:
    """Сбросить кэш настроек ядра и HH-сервиса (после ``monkeypatch.setenv``)."""
    from backend.settings import get_settings
    from jobdesk_hh.settings import get_hh_settings

    get_settings.cache_clear()
    get_hh_settings.cache_clear()


@contextmanager
def hh_service_stack(monkeypatch):
    """HH-сервис в процессе теста, связанный с ядром в обе стороны (platform/internal-services).

    Ядро → сервис и сервис → ядро ходят через отдельные ``TestClient`` без контекста: у каждого
    запроса свой portal, поток inprocess-sync не делит клиента с потоком теста. Lifespan
    сервиса (миграции схемы hh, recover) поднимает ``TestClient`` в контексте. Lifespan ядра
    запускает вызывающий (``api_client`` или свой ``TestClient(app)``).
    """
    import httpx
    from fastapi.testclient import TestClient

    from backend.main import app as core_app
    from backend.services import hh_service_client
    from jobdesk_hh import core_client as hh_core_client
    from jobdesk_hh.api import app as hh_app
    from jobdesk_hh.db.session import reset_database
    from jobdesk_hh.queue import reset_redis_connection

    monkeypatch.setenv("HH_SYNC_JOB_BACKEND", "inprocess")
    monkeypatch.setenv("HH_SERVICE_METRICS_PORT", "0")
    monkeypatch.setenv("HH_MAINTENANCE_INTERVAL_SECONDS", "0")
    clear_settings_caches()
    reset_database()
    with TestClient(hh_app) as hh:
        hh_core_client.set_http_client_override(TestClient(core_app))
        hh_service_client.set_client_overrides(
            TestClient(hh_app),
            lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=hh_app), base_url="http://hh-service"),
        )
        try:
            yield hh
        finally:
            hh_core_client.set_http_client_override(None)
            hh_service_client.set_client_overrides(None)
    reset_database()
    reset_redis_connection()


@pytest.fixture
def hh_client(api_client, monkeypatch):
    """``api_client`` ядра с подключённым HH-сервисом; значение — ``TestClient`` самого сервиса."""
    with hh_service_stack(monkeypatch) as hh:
        yield hh
