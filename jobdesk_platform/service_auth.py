"""Сервисный токен: FastAPI-зависимость для эндпоинтов, которые вызывают только другие сервисы."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import Request

from jobdesk_platform.errors import CODE_NOT_AUTHENTICATED, http_error


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def tokens_match(presented: str | None, expected: str | None) -> bool:
    """Сравнение без утечки по времени; пустой ожидаемый токен не совпадает ни с чем."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def require_service_token(expected_token: Callable[[], str | None]) -> Callable[[Request], None]:
    """Зависимость: ``Authorization: Bearer <токен>`` должен совпасть с ``expected_token()``.

    Ожидаемый токен читается на каждый запрос (настройки под ``lru_cache`` сбрасываются в тестах).
    Иначе — 401 ``NOT_AUTHENTICATED``; JWT пользователя и персональный токен сюда не подходят.
    """

    def dependency(request: Request) -> None:
        if not tokens_match(bearer_token(request), expected_token()):
            raise http_error(
                401,
                CODE_NOT_AUTHENTICATED,
                "Нужен сервисный токен",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return dependency
