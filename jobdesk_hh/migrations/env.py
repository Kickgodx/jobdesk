"""Alembic-окружение цепочки HH-сервиса: схема ``hh``, версия в ``hh.alembic_version``.

Запускается из ``jobdesk_hh.migrate.upgrade_head``: URL — ``config.attributes["url"]``
(не ``sqlalchemy.url``: ``ConfigParser`` интерполирует ``%`` в пароле).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from jobdesk_hh.db.models import HH_SCHEMA, Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(config.attributes["url"], poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Таблица версий живёт в схеме hh — схема нужна до того, как Alembic её создаст.
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {HH_SCHEMA}"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=HH_SCHEMA,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Цепочка HH применяется только online (jobdesk_hh.migrate)")
run_migrations_online()
