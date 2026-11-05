"""Baseline схемы hh: кэш выдачи и джобы HH sync.

Таблицы создаются только если их нет — в том виде, в каком их оставляет миграция ядра
054 (перенос из ``public``). Ядро стартовало первым — здесь ничего не меняется; HH-сервис
первым на пустой базе — таблицы создаются пустыми, а 054 потом сольёт в них строки из
``public``. Имена индексов и ограничений — те же, что у перенесённых таблиц.

Revision ID: hh_0001_baseline
Revises:
Create Date: 2026-11-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "hh_0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS hh")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hh.hh_listings (
            hh_id VARCHAR(32) NOT NULL,
            name VARCHAR(512) NOT NULL,
            employer VARCHAR(256),
            area VARCHAR(256),
            alternate_url TEXT,
            published_at TIMESTAMPTZ,
            content_hash VARCHAR(64) NOT NULL,
            salary_json JSON,
            snippet_json JSON,
            raw_json JSON,
            detail_fetched_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT hh_listings_pkey PRIMARY KEY (hh_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_hh_listings_detail_fetched_at ON hh.hh_listings (detail_fetched_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hh_listings_last_seen_at ON hh.hh_listings (last_seen_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hh.hh_sync_jobs (
            id UUID NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            params JSON NOT NULL,
            result JSON,
            progress JSON,
            progress_updated_at TIMESTAMPTZ,
            error_message TEXT,
            requested_by INTEGER,
            rq_job_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            lease_owner VARCHAR(64),
            lease_until TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested BOOLEAN NOT NULL DEFAULT false,
            CONSTRAINT pk_hh_sync_jobs PRIMARY KEY (id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_hh_sync_jobs_requested_by ON hh.hh_sync_jobs (requested_by)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hh_sync_jobs_rq_job_id ON hh.hh_sync_jobs (rq_job_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hh_sync_jobs_status ON hh.hh_sync_jobs (status)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_hh_sync_jobs_one_active ON hh.hh_sync_jobs ((true)) "
        "WHERE status IN ('queued', 'running')"
    )


def downgrade() -> None:
    # Данные HH-сервиса не удаляются откатом baseline: таблицы могли прийти из ядра (054).
    pass
