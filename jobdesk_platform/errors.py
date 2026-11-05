"""Типизированные ошибки API: detail = {code, message, retryable}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobdesk.config.loader import ConfigLoadError

CODE_CONFLICT_ACTIVE_RUN = "CONFLICT_ACTIVE_RUN"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_VALIDATION = "VALIDATION"
CODE_NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
CODE_INTERNAL = "INTERNAL"
CODE_IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
CODE_LLM_QUOTA_EXCEEDED = "LLM_QUOTA_EXCEEDED"
# 503: все места обработки процесса API заняты дольше API_QUEUE_TIMEOUT_SECONDS (backend/overload.py).
CODE_OVERLOADED = "OVERLOADED"
CODE_EMAIL_TAKEN = "EMAIL_TAKEN"
CODE_CONFIG_INVALID = "CONFIG_INVALID"

_LOG = logging.getLogger(__name__)


class AppError(Exception):
    """Доменное исключение → HTTP с структурированным detail."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def error_detail(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


def http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=error_detail(code, message, retryable=retryable),
        headers=headers,
    )


def _code_for_http_status(status_code: int) -> str:
    if status_code == 401:
        return CODE_NOT_AUTHENTICATED
    if status_code == 403:
        return CODE_FORBIDDEN
    if status_code == 404:
        return CODE_NOT_FOUND
    if status_code == 422:
        return CODE_VALIDATION
    if status_code == 429:
        return CODE_RATE_LIMITED
    if status_code >= 500:
        return CODE_INTERNAL
    return CODE_VALIDATION


def _normalize_http_detail(status_code: int, detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return {
            "code": str(detail["code"]),
            "message": str(detail["message"]),
            "retryable": bool(detail.get("retryable", False)),
        }
    if isinstance(detail, str):
        msg = detail
    elif isinstance(detail, list):
        msg = "Ошибка валидации запроса"
    else:
        msg = str(detail) if detail is not None else _code_for_http_status(status_code)
    return error_detail(_code_for_http_status(status_code), msg, retryable=False)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.code, exc.message, retryable=exc.retryable)},
    )


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": _normalize_http_detail(exc.status_code, exc.detail)},
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
        msg = first.get("msg", "validation error")
        message = f"{loc}: {msg}" if loc else str(msg)
    else:
        message = "Ошибка валидации запроса"
    return JSONResponse(
        status_code=422,
        content={"detail": error_detail(CODE_VALIDATION, message, retryable=False)},
    )


async def config_load_error_handler(_request: Request, exc: ConfigLoadError) -> JSONResponse:
    """Незагружаемый config.yaml — одинаково для всех эндпоинтов; повтор не поможет, пока файл не исправят."""
    _LOG.warning("config.yaml не загружается: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": error_detail(CODE_CONFIG_INVALID, str(exc), retryable=False)},
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    _LOG.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "detail": error_detail(
                CODE_INTERNAL,
                "Внутренняя ошибка сервера",
                retryable=True,
            )
        },
    )


def register_exception_handlers(app: Any) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ConfigLoadError, config_load_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
