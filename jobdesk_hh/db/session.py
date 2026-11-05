"""Подключение HH-сервиса к базе: та же база, что у ядра, данные — в схеме ``hh``."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from jobdesk_hh.settings import get_hh_settings
from jobdesk_platform.db import Database

# Один sync за раз + поток lease/прогресса + API: 2 постоянных и 4 сверху с запасом.
_POOL_SIZE = 2
_MAX_OVERFLOW = 4

_lock = threading.Lock()
_database: Database | None = None


def get_database() -> Database:
    global _database
    with _lock:
        if _database is None:
            _database = Database(get_hh_settings().database_url, pool_size=_POOL_SIZE, max_overflow=_MAX_OVERFLOW)
        return _database


def reset_database() -> None:
    """Закрыть пул (тесты, смена DATABASE_URL)."""
    global _database
    with _lock:
        db, _database = _database, None
    if db is not None:
        db.dispose()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    with get_database().session_scope() as session:
        yield session
