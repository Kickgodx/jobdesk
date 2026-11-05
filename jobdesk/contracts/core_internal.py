"""Внутренний API ядра (``/internal/v1``): вакансии для вынесенных сервисов.

Одни и те же модели валидируют запрос на стороне ядра и собирают его на стороне сервиса.
Вакансия — доменная модель ``jobdesk.models.vacancy.Vacancy`` как есть.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobdesk.models.vacancy import Vacancy

INTERNAL_PREFIX = "/internal/v1"
VACANCIES_LOOKUP_PATH = f"{INTERNAL_PREFIX}/vacancies/lookup"
HH_VACANCIES_PATH = f"{INTERNAL_PREFIX}/sources/hh/vacancies"

# Ключей в одном lookup и вакансий в одной пачке записи.
MAX_BATCH = 500


class VacancyLookupRequest(BaseModel):
    source_post_keys: list[str] = Field(min_length=1, max_length=MAX_BATCH)


class VacancyLookupResponse(BaseModel):
    """Найденные вакансии; ключей, которых нет в базе, в ответе нет."""

    vacancies: list[Vacancy]


class VacancyBatch(BaseModel):
    vacancies: list[Vacancy] = Field(min_length=1, max_length=MAX_BATCH)


class VacancyUpsertResult(BaseModel):
    inserted: int = 0
    updated_existing: int = 0
