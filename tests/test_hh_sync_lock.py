"""Advisory-lock HH sync живёт на выделенном соединении, а не на соединении сессии."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from jobdesk.config.schema import AppConfig, HhConfig, HhSearchConfig
from jobdesk_hh import sync as hh_sync_service
from jobdesk_hh.db import listings as hh_repo
from jobdesk_hh.settings import HhSettings


class _FakeConn:
    def __init__(self, *, got: bool = True, unlock_error: Exception | None = None) -> None:
        self.got = got
        self.unlock_error = unlock_error
        self.options: dict = {}
        self.sql: list[str] = []
        self.closed = False
        self.invalidated = False

    def execution_options(self, **options):
        self.options = options
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if "pg_advisory_unlock" in sql and self.unlock_error is not None:
            raise self.unlock_error
        return SimpleNamespace(scalar=lambda: self.got)

    def invalidate(self) -> None:
        self.invalidated = True

    def close(self) -> None:
        self.closed = True


def _pg_session(conn: _FakeConn) -> MagicMock:
    engine = MagicMock()
    engine.engine = engine
    engine.connect.return_value = conn
    session = MagicMock()
    session.get_bind.return_value = engine
    return session


def test_lock_is_held_on_dedicated_autocommit_connection():
    conn = _FakeConn()
    session = _pg_session(conn)
    lock = hh_repo.HhSyncDbLock(session)

    assert lock.try_acquire() is True
    # Sync коммитит сессию по ходу работы — на lock это влиять не должно.
    session.commit()
    assert conn.closed is False
    lock.release()

    session.execute.assert_not_called()
    assert conn.options == {"isolation_level": "AUTOCOMMIT"}
    assert ["pg_try_advisory_lock" in conn.sql[0], "pg_advisory_unlock" in conn.sql[1]] == [True, True]
    assert conn.closed is True
    assert conn.invalidated is False


def test_busy_lock_closes_connection_and_release_is_noop():
    conn = _FakeConn(got=False)
    lock = hh_repo.HhSyncDbLock(_pg_session(conn))

    assert lock.try_acquire() is False
    assert conn.closed is True

    lock.release()
    assert len(conn.sql) == 1  # unlock не выполнялся: lock не наш


def test_failed_unlock_invalidates_connection():
    """Соединение с неснятым lock в пул не возвращается — иначе 409 до рестарта."""
    conn = _FakeConn(unlock_error=RuntimeError("connection reset"))
    lock = hh_repo.HhSyncDbLock(_pg_session(conn))
    assert lock.try_acquire() is True

    lock.release()

    assert conn.invalidated is True
    assert conn.closed is True


def test_lock_excludes_second_holder_on_real_postgres(db_session):
    first = hh_repo.HhSyncDbLock(db_session)
    second = hh_repo.HhSyncDbLock(db_session)
    assert first.try_acquire() is True
    # Коммит сессии не отпускает lock: он живёт на своём соединении.
    db_session.commit()
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()


class _RecordingLock:
    instances: ClassVar[list[_RecordingLock]] = []
    acquired = True

    def __init__(self, session) -> None:
        self.released = 0
        _RecordingLock.instances.append(self)

    def try_acquire(self) -> bool:
        return _RecordingLock.acquired

    def release(self) -> None:
        self.released += 1


@pytest.fixture
def recording_lock(monkeypatch):
    _RecordingLock.instances = []
    _RecordingLock.acquired = True
    monkeypatch.setattr(hh_sync_service.listings, "HhSyncDbLock", _RecordingLock)
    return _RecordingLock


def _sync():
    config = AppConfig(
        hh=HhConfig(enabled=True, search=HhSearchConfig(text="Python", professional_role=["124"], per_page=100))
    )
    settings = HhSettings(HH_API_TOKEN="t", HH_CONTACT_EMAIL="a@b.c")
    return hh_sync_service.run_hh_sync(MagicMock(), config=config, settings=settings, core=MagicMock())


def test_sync_releases_lock_when_it_fails(monkeypatch, recording_lock):
    def boom(*args, **kwargs):
        raise RuntimeError("hh down")

    monkeypatch.setattr(hh_sync_service, "HHClient", MagicMock())
    monkeypatch.setattr(hh_sync_service, "_collect_shorts", boom)

    with pytest.raises(RuntimeError, match="hh down"):
        _sync()

    assert [lock.released for lock in recording_lock.instances] == [1]
    # Процессный lock тоже снят: следующий sync доходит до работы, а не до 409.
    with pytest.raises(RuntimeError, match="hh down"):
        _sync()


def test_sync_conflict_when_db_lock_is_busy(recording_lock):
    recording_lock.acquired = False

    with pytest.raises(hh_sync_service.HhSyncConflict):
        _sync()

    assert hh_sync_service._SYNC_LOCK.acquire(blocking=False) is True
    hh_sync_service._SYNC_LOCK.release()
