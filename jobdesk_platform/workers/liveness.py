"""Heartbeat воркера: «джобы выполняются», а не только «процесс жив».

Бьют два источника:

- цикл RQ (``LivenessWorker.heartbeat``) — в простое раз в ``dequeue_timeout``
  (``RQ_WORKER_TTL - 15`` с) и перед каждой джобой;
- lease-heartbeat выполняемой джобы — только после успешного продления lease в БД.

Время последнего удара — gauge ``worker_heartbeat_timestamp_seconds`` в ``/metrics``:
по нему отвечает Docker healthcheck (``jobdesk_platform.workers.healthcheck``) и можно алертить.
Docker сам unhealthy-контейнер не перезапускает, поэтому watchdog завершает процесс,
если heartbeat молчит дольше ``WORKER_STALL_EXIT_SECONDS``: ``restart: unless-stopped``
поднимет воркер, а recover вернёт его джобу в очередь.

Не ловится: зависший код джобы при живом потоке lease — его снимает job_timeout RQ.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable

from prometheus_client import Gauge
from rq import SimpleWorker

from jobdesk_platform.workers.healthcheck import HEARTBEAT_METRIC

_LOG = logging.getLogger(__name__)

# Простой RQ: heartbeat раз в RQ_WORKER_TTL - 15 = 45 с (по умолчанию rq — 405 с).
RQ_WORKER_TTL = 60
# Код выхода watchdog: отличим в `docker ps` / логах от падения по исключению.
STALL_EXIT_CODE = 70

WORKER_HEARTBEAT = Gauge(
    HEARTBEAT_METRIC,
    "Последний heartbeat воркера: цикл RQ или продление lease джобы (unix time)",
)

_last_beat = time.time()


def beat() -> None:
    global _last_beat
    _last_beat = time.time()
    WORKER_HEARTBEAT.set(_last_beat)


def seconds_since_beat() -> float:
    return time.time() - _last_beat


class LivenessWorker(SimpleWorker):
    """SimpleWorker, чей heartbeat в Redis заодно отмечается в ``/metrics``."""

    def heartbeat(self, *args, **kwargs) -> None:
        super().heartbeat(*args, **kwargs)
        beat()


def start_watchdog(
    stall_seconds: float,
    *,
    name: str,
    exit_fn: Callable[[int], object] = os._exit,
) -> threading.Event | None:
    """Daemon-поток: heartbeat молчит дольше ``stall_seconds`` → ``exit_fn(STALL_EXIT_CODE)``.

    ``os._exit``, а не ``sys.exit``: главный поток завис, ``finally`` в нём не выполнится.
    ``stall_seconds <= 0`` — watchdog выключен (возвращает ``None``).
    """
    if stall_seconds <= 0:
        return None
    beat()
    stop = threading.Event()
    interval = max(1.0, min(30.0, stall_seconds / 4))

    def _loop() -> None:
        while not stop.wait(timeout=interval):
            silent = seconds_since_beat()
            if silent <= stall_seconds:
                continue
            _LOG.critical(
                "%s: нет heartbeat %.0f с (порог %.0f) — воркер завис, завершаем процесс для рестарта",
                name,
                silent,
                stall_seconds,
            )
            for handler in logging.getLogger().handlers:
                with contextlib.suppress(Exception):
                    handler.flush()
            exit_fn(STALL_EXIT_CODE)
            return

    threading.Thread(target=_loop, name=f"{name}-watchdog", daemon=True).start()
    return stop
