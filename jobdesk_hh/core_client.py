"""Клиент внутреннего API ядра: единственный путь HH-сервиса к вакансиям.

Оба вызова идемпотентны (чтение; upsert по ``source_post_key``) — ``ServiceClient`` их
повторяет при сбоях сети и 502/503/504. Пачки больше ``MAX_BATCH`` режутся на части.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence

import httpx
from pydantic import ValidationError

from jobdesk.contracts.core_internal import (
    HH_VACANCIES_PATH,
    MAX_BATCH,
    VACANCIES_LOOKUP_PATH,
    VacancyBatch,
    VacancyLookupRequest,
    VacancyLookupResponse,
    VacancyUpsertResult,
)
from jobdesk.models.vacancy import Vacancy
from jobdesk_hh.settings import get_hh_settings
from jobdesk_platform.service_client import ServiceClient, UpstreamUnavailable


def _chunks[T](items: Sequence[T], size: int = MAX_BATCH) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


class CoreClient:
    def __init__(self, client: ServiceClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.close()

    def lookup(self, source_post_keys: Sequence[str]) -> dict[str, Vacancy]:
        """Сохранённые вакансии по ``source_post_key``; неизвестных ключей в ответе нет."""
        keys = list(dict.fromkeys(k for k in source_post_keys if k))
        found: dict[str, Vacancy] = {}
        for chunk in _chunks(keys):
            body = VacancyLookupRequest(source_post_keys=chunk).model_dump(mode="json")
            data = self._client.request_json("POST", VACANCIES_LOOKUP_PATH, json=body, idempotent=True)
            try:
                response = VacancyLookupResponse.model_validate(data)
            except ValidationError as e:
                raise UpstreamUnavailable(f"core: ответ lookup не по контракту: {e}") from e
            found.update({v.source_post_key: v for v in response.vacancies})
        return found

    def upsert_hh(self, vacancies: Sequence[Vacancy]) -> VacancyUpsertResult:
        """Записать вакансии HH; каждая часть пачки — одна транзакция ядра."""
        total = VacancyUpsertResult()
        for chunk in _chunks(list(vacancies)):
            body = VacancyBatch(vacancies=chunk).model_dump(mode="json")
            data = self._client.request_json("POST", HH_VACANCIES_PATH, json=body, idempotent=True)
            try:
                part = VacancyUpsertResult.model_validate(data)
            except ValidationError as e:
                raise UpstreamUnavailable(f"core: ответ записи не по контракту: {e}") from e
            total = VacancyUpsertResult(
                inserted=total.inserted + part.inserted,
                updated_existing=total.updated_existing + part.updated_existing,
            )
        return total


_override_lock = threading.Lock()
_http_override: httpx.Client | None = None


def set_http_client_override(client: httpx.Client | None) -> None:
    """Тесты: ходить в ядро через готовый клиент (``TestClient`` приложения ядра)."""
    global _http_override
    with _override_lock:
        _http_override = client


def core_client() -> CoreClient:
    """Новый клиент ядра по настройкам процесса; закрывать после sync (``close``)."""
    settings = get_hh_settings()
    return CoreClient(
        ServiceClient(
            settings.core_internal_url,
            settings.core_internal_token or "",
            service="core",
            timeout=settings.core_timeout_seconds,
            client=_http_override,
        )
    )
