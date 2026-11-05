"""Перенесён в ``jobdesk_platform.pubsub_hub``; здесь — алиас того же модуля для старых импортов.

Ядро регистрирует фабрику Redis по умолчанию — ``backend.queue.rq_conn.get_async_redis_client``,
связанную поздно: тесты подменяют её в ``rq_conn``.
"""

import sys

from jobdesk_platform import pubsub_hub as _impl


def _core_async_redis():
    from backend.queue import rq_conn

    return rq_conn.get_async_redis_client()


_impl.set_default_redis_factory(_core_async_redis)
sys.modules[__name__] = _impl
