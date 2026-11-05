"""Внутренний API ядра для вынесенных сервисов (``/internal/v1``, вне публичного ``/api/v1``).

Только с сервисным токеном ``CORE_INTERNAL_TOKEN``; пользовательский вход (JWT, ``tgj_…``)
здесь не действует. Наружу не проксируется: Vite и reverse-proxy пропускают только ``/api``.
Контракт — ``jobdesk.contracts.core_internal``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import vacancy_repository as vacancy_repo
from backend.db.session import get_db
from backend.db.vacancy_mapper import row_to_vacancy
from backend.settings import get_settings
from jobdesk.config.loader import load_config
from jobdesk.contracts.core_internal import (
    INTERNAL_PREFIX,
    VacancyBatch,
    VacancyLookupRequest,
    VacancyLookupResponse,
    VacancyUpsertResult,
)
from jobdesk_platform.service_auth import require_service_token

router = APIRouter(
    prefix=INTERNAL_PREFIX,
    tags=["internal"],
    dependencies=[Depends(require_service_token(lambda: get_settings().core_internal_token))],
    include_in_schema=False,
)


@router.post("/vacancies/lookup", response_model=VacancyLookupResponse)
def lookup_vacancies(body: VacancyLookupRequest, db: Session = Depends(get_db)) -> VacancyLookupResponse:
    """Вакансии по ``source_post_key``, включая архивные и дубли."""
    rows = vacancy_repo.get_vacancies_by_source_post_keys(db, body.source_post_keys)
    return VacancyLookupResponse(vacancies=[row_to_vacancy(row) for row in rows.values()])


@router.post("/sources/hh/vacancies", response_model=VacancyUpsertResult)
def upsert_hh_vacancies(body: VacancyBatch, db: Session = Depends(get_db)) -> VacancyUpsertResult:
    """Пачка вакансий HH — одной транзакцией, по правилам записи HH (FX, возврат из архива)."""
    cfg = load_config(get_settings().config_path)
    stats = vacancy_repo.upsert_hh_vacancies(db, body.vacancies, fx_config=cfg.fx)
    return VacancyUpsertResult(inserted=stats.inserted, updated_existing=stats.updated_existing)
