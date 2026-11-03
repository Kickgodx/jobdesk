"""jobdesk_platform.db: URL по умолчанию, отказ не-Postgres, session_scope."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from jobdesk_platform.db import Database, default_database_url, require_postgres_url


def test_default_url_uses_ipv4_loopback_and_quotes_password():
    url = default_database_url("p@ss/w")
    assert url == "postgresql+psycopg://tgjobs:p%40ss%2Fw@127.0.0.1:5432/tgjobs"
    assert default_database_url(None).endswith(":tgjobs@127.0.0.1:5432/tgjobs")


def test_non_postgres_url_rejected_with_env_name():
    with pytest.raises(ValueError, match="HH_DATABASE_URL"):
        require_postgres_url("sqlite:///x.db", env_name="HH_DATABASE_URL")


def test_session_scope_commits_and_rolls_back(pg_db):
    db = Database(pg_db, pool_size=1, max_overflow=0)
    try:
        with db.session_scope() as s:
            s.execute(sa.text("CREATE TABLE IF NOT EXISTS platform_probe (v jsonb)"))
            s.execute(sa.text("INSERT INTO platform_probe VALUES (CAST(:v AS jsonb))"), {"v": '{"k": "й"}'})
        with pytest.raises(RuntimeError), db.session_scope() as s:
            s.execute(sa.text("INSERT INTO platform_probe VALUES ('{}')"))
            raise RuntimeError("boom")
        with db.session_scope() as s:
            rows = s.execute(sa.text("SELECT v FROM platform_probe")).scalars().all()
        assert rows == [{"k": "й"}]
    finally:
        with db.engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS platform_probe"))
        db.dispose()


def test_dispose_recreates_engine(pg_db):
    db = Database(pg_db)
    first = db.engine
    db.dispose()
    assert db.engine is not first
    db.dispose()
