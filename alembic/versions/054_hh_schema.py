"""HH-таблицы переезжают в схему ``hh`` — данными владеет HH-сервис (``jobdesk_hh``).

``hh_listings`` и ``hh_sync_jobs`` переносятся вместе с данными и индексами
(``ALTER TABLE … SET SCHEMA`` меняет только метаданные). Если HH-сервис успел применить свою
цепочку раньше ядра и создать пустые ``hh.*`` (порядок первого старта не задан), строки
из ``public`` сливаются в них, а старые таблицы удаляются — в базе остаётся одна копия.
FK ``requested_by → users`` снимается: сервис хранит id пользователя без связи со схемой ядра.

Downgrade возвращает таблицы в ``public`` без FK.

Revision ID: 054_hh_schema
Revises: 053_strip_grade_from_roles
Create Date: 2026-11-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "054_hh_schema"
down_revision: Union[str, None] = "053_strip_grade_from_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCHEMA = "hh"
# Колонки явно: слияние не должно зависеть от порядка колонок в двух копиях таблицы.
_COLUMNS = {
    "hh_listings": (
        "hh_id, name, employer, area, alternate_url, published_at, content_hash, salary_json, "
        "snippet_json, raw_json, detail_fetched_at, last_seen_at, created_at, updated_at"
    ),
    "hh_sync_jobs": (
        "id, status, params, result, progress, progress_updated_at, error_message, requested_by, "
        "rq_job_id, created_at, started_at, finished_at, lease_owner, lease_until, attempts, cancel_requested"
    ),
}


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
    for table, columns in _COLUMNS.items():
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('public.{table}') IS NULL THEN
                    RETURN;
                END IF;
                IF to_regclass('{_SCHEMA}.{table}') IS NULL THEN
                    ALTER TABLE public.{table} SET SCHEMA {_SCHEMA};
                ELSE
                    INSERT INTO {_SCHEMA}.{table} ({columns})
                    SELECT {columns} FROM public.{table}
                    ON CONFLICT DO NOTHING;
                    DROP TABLE public.{table};
                END IF;
            END $$;
            """
        )
    op.execute(f"ALTER TABLE {_SCHEMA}.hh_sync_jobs DROP CONSTRAINT IF EXISTS fk_hh_sync_jobs_requested_by")


def downgrade() -> None:
    for table in _COLUMNS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('{_SCHEMA}.{table}') IS NOT NULL AND to_regclass('public.{table}') IS NULL THEN
                    ALTER TABLE {_SCHEMA}.{table} SET SCHEMA public;
                END IF;
            END $$;
            """
        )
