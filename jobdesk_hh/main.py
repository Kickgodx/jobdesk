"""Entrypoint ``jobdesk-hh-service``: HTTP API HH-сервиса (один процесс uvicorn)."""

from __future__ import annotations

import os

DEFAULT_SERVICE_NAME = "jobdesk-hh-service"


def setup_service_tracing() -> bool:
    from jobdesk_hh.api import app
    from jobdesk_hh.settings import get_hh_settings
    from jobdesk_platform.tracing import setup_tracing

    settings = get_hh_settings()
    return setup_tracing(
        app,
        enabled=settings.otel_enabled,
        sample_ratio=settings.otel_traces_sample_ratio,
        exclude_paths=frozenset({"/health"}),
        default_service_name=DEFAULT_SERVICE_NAME,
    )


def main() -> None:
    import uvicorn

    from jobdesk.utils.env_loader import try_load_dotenv
    from jobdesk.utils.logger import setup_logging

    setup_logging()
    try_load_dotenv()
    os.environ.setdefault("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    from jobdesk_hh.api import app
    from jobdesk_hh.settings import get_hh_settings

    setup_service_tracing()
    settings = get_hh_settings()
    try:
        uvicorn.run(app, host=settings.hh_service_host, port=settings.hh_service_port, log_config=None)
    finally:
        from jobdesk_platform.tracing import shutdown_tracing

        shutdown_tracing()


if __name__ == "__main__":
    main()
