"""Общие куски SSE-стримов (прогоны, LLM-джобы, HH sync)."""

from __future__ import annotations

import time

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Комментарий-пинг: прокси не закрывают «тихое» соединение по idle-timeout.
SSE_PING = ": ping\n\n"
SSE_DONE = "event: done\ndata: {}\n\n"

HEARTBEAT_INTERVAL_S = 15.0
# Ожидание сообщения из pub/sub; между ними — пинг и редкая сверка с БД.
PUBSUB_WAIT_S = 1.0
# Сверка статуса с БД в тишине pub/sub: страхует от пропущенного «done»
# (событие ушло до подписки или Redis его потерял).
IDLE_DB_CHECK_S = 5.0
# Опрос БД без Redis.
DB_POLL_INTERVAL_S = 1.0


def sse_event(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


class Ticker:
    """Срабатывает не чаще раза в ``interval`` секунд (монотонные часы)."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = time.monotonic()

    def reset(self) -> None:
        self._last = time.monotonic()

    def due(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False
