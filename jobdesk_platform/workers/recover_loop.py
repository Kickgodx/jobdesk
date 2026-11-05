"""Периодический recover stuck jobs: в RQ-воркерах и в процессе, принимающем джобы, при ``inprocess``."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_LOG = logging.getLogger(__name__)


def rq_job_still_live(rq_job_id: str, connection) -> bool:
    """True, если RQ-задача ещё есть и не в finished/failed."""
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        job = Job.fetch(rq_job_id, connection=connection)
    except NoSuchJobError:
        return False
    return not (job.is_finished or job.is_failed)


def start_periodic_recover(
    recover_fn: Callable[[], int],
    *,
    interval_seconds: int,
    name: str,
) -> threading.Event:
    """Запустить daemon-thread; вернуть Event для остановки."""
    stop = threading.Event()
    interval = max(15, int(interval_seconds))

    def _loop() -> None:
        while not stop.wait(timeout=interval):
            try:
                n = recover_fn()
                if n:
                    _LOG.info("%s: periodic recover requeued=%s", name, n)
            except Exception as e:
                _LOG.warning("%s: periodic recover failed: %s", name, e)

    threading.Thread(target=_loop, name=name, daemon=True).start()
    return stop
