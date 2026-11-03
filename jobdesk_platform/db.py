"""Подключение к Postgres для процессов JobDesk: движок, фабрика сессий, ``session_scope``.

Ядро держит свои пулы по ролям в ``backend.db.session``; сервисы создают ``Database``
со своим размером пула. Движок ленивый: создаётся при первом обращении.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from urllib.parse import quote

import sqlalchemy
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def default_database_url(postgres_password: str | None) -> str:
    """URL по умолчанию — Postgres из compose на хосте.

    127.0.0.1, а не localhost: compose публикует порт только на IPv4-loopback, а localhost
    сначала резолвится в ::1 — на Windows psycopg ждёт там весь connect timeout.
    """
    password = quote(postgres_password or "tgjobs", safe="")
    return f"postgresql+psycopg://tgjobs:{password}@127.0.0.1:5432/tgjobs"


def require_postgres_url(url: str, *, env_name: str = "DATABASE_URL") -> str:
    """SQL, блокировки и миграции написаны только под Postgres — другой диалект отклоняется."""
    if not url.startswith("postgresql"):
        raise ValueError(f"{env_name}: поддерживается только PostgreSQL (postgresql+psycopg://…)")
    return url


def json_serializer(obj: object) -> str:
    """UTF-8 JSON без \\uXXXX — читаемее в БД и компактнее для кириллицы."""
    return json.dumps(obj, ensure_ascii=False)


class Database:
    """Ленивый движок + фабрика сессий для одного URL."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 2,
        max_overflow: int = 2,
        pool_timeout: float = 30.0,
    ) -> None:
        self.url = url
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            # Через модуль, а не `from sqlalchemy import create_engine`: OTel-инструментация
            # подменяет `sqlalchemy.create_engine` уже после импорта этого модуля.
            self._engine = sqlalchemy.create_engine(
                self.url,
                json_serializer=json_serializer,
                pool_pre_ping=True,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_timeout=self._pool_timeout,
            )
            self._factory = sessionmaker(bind=self._engine, autoflush=False, autocommit=False)
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        _ = self.engine
        assert self._factory is not None
        return self._factory

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Сессия с commit при успехе и rollback при исключении."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Закрыть пул (тесты, смена URL); следующий доступ создаст движок заново."""
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._factory = None
