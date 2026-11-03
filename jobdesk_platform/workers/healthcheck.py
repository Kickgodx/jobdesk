"""Docker healthcheck воркера: ``python -m jobdesk_platform.workers.healthcheck --port 9101``.

Здоров — если ``/metrics`` отвечает и heartbeat (``worker_heartbeat_timestamp_seconds``)
не старше ``--max-age`` (по умолчанию ``WORKER_HEARTBEAT_MAX_AGE_SECONDS`` или 180 с).
Ответ порта сам по себе значит только «процесс жив». Без импорта настроек процесса:
проверка запускается каждые 15 с и должна быть дешёвой.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

HEARTBEAT_METRIC = "worker_heartbeat_timestamp_seconds"
DEFAULT_MAX_AGE_SECONDS = 180.0


def heartbeat_from_metrics(text: str) -> float | None:
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == HEARTBEAT_METRIC:
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def check(metrics_text: str, *, max_age: float, now: float | None = None) -> tuple[bool, str]:
    ts = heartbeat_from_metrics(metrics_text)
    if ts is None or ts <= 0:
        return False, f"нет {HEARTBEAT_METRIC} в /metrics"
    age = (now if now is not None else time.time()) - ts
    if age > max_age:
        return False, f"heartbeat {age:.0f} с назад (порог {max_age:.0f})"
    return True, f"heartbeat {age:.0f} с назад"


def _default_max_age() -> float:
    try:
        return float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS") or DEFAULT_MAX_AGE_SECONDS)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="порт /metrics воркера")
    parser.add_argument("--max-age", type=float, default=None, help="допустимый возраст heartbeat, с")
    args = parser.parse_args(argv)
    max_age = args.max_age if args.max_age is not None else _default_max_age()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/metrics", timeout=4) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"unhealthy: /metrics недоступен: {e}")
        return 1
    ok, reason = check(text, max_age=max_age)
    print(("healthy: " if ok else "unhealthy: ") + reason)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
