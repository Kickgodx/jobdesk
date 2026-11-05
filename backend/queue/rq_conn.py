"""Подключение к Redis и очереди RQ ``llm`` / ``parse`` (очередь ``hh`` — у HH-сервиса)."""

from __future__ import annotations

import contextlib
from typing import Any

from backend.settings import get_settings

LLM_QUEUE_NAME = "llm"
PARSE_QUEUE_NAME = "parse"

_connection: Any | None = None


def get_redis_connection(*, force_new: bool = False):
    """``redis.Redis`` из ``REDIS_URL`` (или fakeredis в тестах через monkeypatch)."""
    global _connection
    if _connection is not None and not force_new:
        return _connection
    settings = get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        raise RuntimeError("REDIS_URL не задан (нужен для LLM_JOB_BACKEND=rq)")
    from redis import Redis

    _connection = Redis.from_url(url)
    return _connection


def get_async_redis_client():
    """Новый ``redis.asyncio.Redis`` из ``REDIS_URL`` — для SSE-хаба (один на event loop)."""
    url = (get_settings().redis_url or "").strip()
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


def get_llm_queue():
    from rq import Queue

    return Queue(LLM_QUEUE_NAME, connection=get_redis_connection())


def llm_queue_length() -> int | None:
    """Число jobs в очереди RQ; ``None`` если Redis недоступен / не rq-режим."""
    try:
        settings = get_settings()
        if settings.llm_job_backend_normalized != "rq" or not (settings.redis_url or "").strip():
            return None
        return int(get_llm_queue().count)
    except Exception:
        return None


def get_parse_queue():
    from rq import Queue

    return Queue(PARSE_QUEUE_NAME, connection=get_redis_connection())


def parse_queue_depth() -> int | None:
    """Число jobs в очереди parse RQ; ``None`` если Redis недоступен / не rq-режим."""
    try:
        settings = get_settings()
        if settings.parse_job_backend_normalized != "rq" or not (settings.redis_url or "").strip():
            return None
        return int(get_parse_queue().count)
    except Exception:
        return None
