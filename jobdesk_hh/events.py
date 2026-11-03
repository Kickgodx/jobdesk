"""Redis Pub/Sub для статуса фонового HH sync (SSE сервиса)."""

from __future__ import annotations

import json
import logging
import uuid

_LOG = logging.getLogger(__name__)

CHANNEL_PREFIX = "hh_sync:"


def channel_for(job_id: uuid.UUID | str) -> str:
    return f"{CHANNEL_PREFIX}{job_id}"


def publish_hh_sync_event(job_id: uuid.UUID | str, *, status: str) -> None:
    """Лучшее усилие: при отсутствии Redis / ошибке — только debug-лог.

    Сообщение — только сигнал «что-то изменилось»: SSE перечитывает джобу из БД.
    """
    try:
        from jobdesk_hh.queue import get_redis_connection
        from jobdesk_hh.settings import get_hh_settings

        if not (get_hh_settings().redis_url or "").strip():
            return
        payload = {"job_id": str(job_id), "status": status}
        get_redis_connection().publish(channel_for(job_id), json.dumps(payload))
    except Exception as e:
        _LOG.debug("publish_hh_sync_event: %s", e)
