"""Redis HH-сервиса: очередь RQ ``hh`` и pub/sub событий sync."""

from __future__ import annotations

import contextlib
from typing import Any

from jobdesk_hh.settings import get_hh_settings

HH_QUEUE_NAME = "hh"

_connection: Any | None = None


def get_redis_connection():
    """``redis.Redis`` из ``REDIS_URL`` (или fakeredis в тестах через monkeypatch)."""
    global _connection
    if _connection is not None:
        return _connection
    url = (get_hh_settings().redis_url or "").strip()
    if not url:
        raise RuntimeError("REDIS_URL не задан (нужен для HH_SYNC_JOB_BACKEND=rq)")
    from redis import Redis

    _connection = Redis.from_url(url)
    return _connection


def get_async_redis_client():
    """Новый ``redis.asyncio.Redis`` из ``REDIS_URL`` — для SSE-хаба сервиса."""
    url = (get_hh_settings().redis_url or "").strip()
    if not url:
        raise RuntimeError("REDIS_URL не задан")
    from redis.asyncio import Redis

    return Redis.from_url(url)


def reset_redis_connection() -> None:
    global _connection
    if _connection is not None:
        with contextlib.suppress(Exception):
            _connection.close()
    _connection = None


def get_hh_queue():
    from rq import Queue

    return Queue(HH_QUEUE_NAME, connection=get_redis_connection())
