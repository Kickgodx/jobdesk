"""Сервисные токены в настройках процессов: dev-значения и обязательность в prod.

Без FastAPI и прочих тяжёлых импортов: модуль читают настройки каждого процесса.
"""

from __future__ import annotations

MIN_SERVICE_TOKEN_LENGTH = 32
# dev-значения одинаковы у вызывающей и принимающей стороны: локальный запуск и тесты
# работают без настройки. В prod токены обязательны (``resolve_service_token``).
DEV_CORE_INTERNAL_TOKEN = "dev-core-internal-token-not-for-production"
DEV_HH_SERVICE_TOKEN = "dev-hh-service-token-not-for-production"


def resolve_service_token(value: str | None, *, env_name: str, is_production: bool, dev_default: str) -> str:
    """Токен из окружения; пусто → dev-значение, а в prod — ошибка, называющая переменную.

    Значение токена в сообщение не попадает.
    """
    token = (value or "").strip()
    if is_production:
        if len(token) < MIN_SERVICE_TOKEN_LENGTH:
            raise ValueError(f"APP_ENV=prod: задайте {env_name} не короче {MIN_SERVICE_TOKEN_LENGTH} символов")
        return token
    return token or dev_default
