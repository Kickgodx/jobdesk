"""Миграции схемы ``hh`` при старте HH-сервиса.

Своя цепочка Alembic (``jobdesk_hh/migrations``, версия — ``hh.alembic_version``) и свой
advisory lock: реплики сервиса применяют миграции по очереди, независимо от миграций ядра.
Ошибка миграции останавливает старт сервиса.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, pool, text

_LOG = logging.getLogger(__name__)

# Ключ pg_advisory_lock миграций HH (≠ ключу миграций ядра 7_242_031_001).
HH_MIGRATION_LOCK_KEY = 7_242_031_011
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


@contextmanager
def migration_lock(url: str) -> Iterator[None]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        # AUTOCOMMIT: session-level lock не держит открытую транзакцию на время DDL.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": HH_MIGRATION_LOCK_KEY})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": HH_MIGRATION_LOCK_KEY})
    finally:
        engine.dispose()


def upgrade_head(url: str) -> None:
    logging.getLogger("alembic").setLevel(logging.WARNING)
    with migration_lock(url):
        command.upgrade(alembic_config(url), "head")
    _LOG.info("Схема hh готова.")
