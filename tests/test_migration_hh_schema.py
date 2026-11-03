"""Перенос HH-таблиц в схему hh (миграция ядра 054 и baseline цепочки HH-сервиса)."""

from __future__ import annotations

import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from tests.conftest import new_user

from backend.db.migrate import alembic_ini_path


def _core_cfg() -> Config:
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    return cfg


def _tables(conn, table: str) -> list[str]:
    return list(
        conn.execute(
            text("SELECT schemaname FROM pg_tables WHERE tablename = :t ORDER BY schemaname"), {"t": table}
        ).scalars()
    )


def _seed_legacy_hh(engine, *, requested_by: int) -> uuid.UUID:
    job_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hh_listings (hh_id, name, content_hash, detail_fetched_at) "
                "VALUES ('101', 'Python dev', 'h1', now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO hh_sync_jobs (id, status, params, requested_by) "
                "VALUES (:id, 'completed', '{\"window\": \"7d\"}', :u)"
            ),
            {"id": job_id, "u": requested_by},
        )
    return job_id


def test_existing_hh_rows_move_to_hh_schema_and_fk_dropped(pg_empty_db):
    cfg = _core_cfg()
    command.upgrade(cfg, "053_strip_grade_from_roles")
    engine = create_engine(pg_empty_db)
    with sessionmaker(bind=engine)() as s:
        user_id = new_user(s)
        s.commit()
    job_id = _seed_legacy_hh(engine, requested_by=user_id)

    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert _tables(conn, "hh_listings") == ["hh"]
        assert _tables(conn, "hh_sync_jobs") == ["hh"]
        assert conn.execute(text("SELECT name FROM hh.hh_listings WHERE hh_id = '101'")).scalar_one() == "Python dev"
        # Удаление пользователя не трогает историю sync: FK на users снят.
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        row = conn.execute(
            text("SELECT requested_by, params FROM hh.hh_sync_jobs WHERE id = :id"), {"id": job_id}
        ).one()
        assert row.requested_by == user_id
        assert row.params == {"window": "7d"}
        # Индекс единственного активного sync переехал вместе с таблицей.
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE schemaname = 'hh' AND indexname = 'uq_hh_sync_jobs_one_active'"
                )
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_downgrade_returns_tables_to_public(pg_empty_db):
    cfg = _core_cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "053_strip_grade_from_roles")
    engine = create_engine(pg_empty_db)
    with engine.connect() as conn:
        assert _tables(conn, "hh_listings") == ["public"]
        assert _tables(conn, "hh_sync_jobs") == ["public"]
    engine.dispose()


# --- порядок первого старта ядра и HH-сервиса ---------------------------------------------


def _hh_upgrade(url: str) -> None:
    from jobdesk_hh.migrate import upgrade_head

    upgrade_head(url)


def _hh_version(conn) -> str:
    return conn.execute(text("SELECT version_num FROM hh.alembic_version")).scalar_one()


def _shape(conn) -> dict[str, list]:
    """Колонки, индексы и ограничения таблиц схемы hh — для сравнения двух путей создания."""
    shape: dict[str, list] = {}
    for table in ("hh_listings", "hh_sync_jobs"):
        shape[f"{table}.columns"] = [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'hh' AND table_name = :t "
                    "ORDER BY column_name"
                ),
                {"t": table},
            )
        ]
        shape[f"{table}.indexes"] = [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'hh' AND tablename = :t ORDER BY 1"
                ),
                {"t": table},
            )
        ]
        shape[f"{table}.constraints"] = [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = ('hh.' || :t)::regclass ORDER BY 1"
                ),
                {"t": table},
            )
        ]
    return shape


def test_service_first_on_empty_db_leaves_single_copy(pg_empty_db):
    _hh_upgrade(pg_empty_db)
    command.upgrade(_core_cfg(), "head")
    engine = create_engine(pg_empty_db)
    with engine.connect() as conn:
        assert _tables(conn, "hh_listings") == ["hh"]
        assert _tables(conn, "hh_sync_jobs") == ["hh"]
        assert _hh_version(conn) == "hh_0001_baseline"
    engine.dispose()


def test_core_first_then_baseline_is_noop(pg_empty_db):
    cfg = _core_cfg()
    command.upgrade(cfg, "053_strip_grade_from_roles")
    engine = create_engine(pg_empty_db)
    with sessionmaker(bind=engine)() as s:
        user_id = new_user(s)
        s.commit()
    _seed_legacy_hh(engine, requested_by=user_id)
    command.upgrade(cfg, "head")
    _hh_upgrade(pg_empty_db)
    _hh_upgrade(pg_empty_db)  # повторный старт — no-op
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM hh.hh_listings")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM hh.hh_sync_jobs")).scalar_one() == 1
        assert _hh_version(conn) == "hh_0001_baseline"
    engine.dispose()


def test_rows_merge_when_service_created_tables_before_core_054(pg_empty_db):
    """База со старыми данными, HH-сервис стартовал раньше миграции ядра и уже писал в hh."""
    cfg = _core_cfg()
    command.upgrade(cfg, "053_strip_grade_from_roles")
    engine = create_engine(pg_empty_db)
    with sessionmaker(bind=engine)() as s:
        user_id = new_user(s)
        s.commit()
    legacy_job = _seed_legacy_hh(engine, requested_by=user_id)
    _hh_upgrade(pg_empty_db)
    new_job = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO hh.hh_listings (hh_id, name, content_hash) VALUES ('202', 'Go dev', 'h2')"))
        conn.execute(
            text("INSERT INTO hh.hh_sync_jobs (id, status, params) VALUES (:id, 'completed', '{}')"), {"id": new_job}
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert _tables(conn, "hh_listings") == ["hh"]
        assert _tables(conn, "hh_sync_jobs") == ["hh"]
        ids = conn.execute(text("SELECT hh_id FROM hh.hh_listings ORDER BY hh_id")).scalars().all()
        assert ids == ["101", "202"]
        jobs = set(conn.execute(text("SELECT id FROM hh.hh_sync_jobs")).scalars())
        assert jobs == {legacy_job, new_job}
    engine.dispose()


def test_baseline_shape_matches_tables_moved_by_core(pg_empty_db):
    command.upgrade(_core_cfg(), "head")
    engine = create_engine(pg_empty_db)
    with engine.connect() as conn:
        moved = _shape(conn)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA hh CASCADE"))
    _hh_upgrade(pg_empty_db)
    with engine.connect() as conn:
        created = _shape(conn)
    engine.dispose()
    assert created == moved
