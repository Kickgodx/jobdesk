"""API HH-сервиса (``/v1``): его вызывает только ядро, проксируя публичные ``/api/v1/hh/*``.

Тела совпадают с публичным контрактом ``/api/v1/hh/*`` — ядро отдаёт их клиенту как есть.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# id пользователя ядра, от имени которого идёт запрос (``requested_by`` sync).
USER_ID_HEADER = "X-JobDesk-User-Id"

STATUS_PATH = "/v1/status"
SYNC_PATH = "/v1/sync"
SYNC_JOBS_PATH = "/v1/sync-jobs"

HhSyncJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


def sync_job_path(job_id: uuid.UUID | str) -> str:
    return f"{SYNC_JOBS_PATH}/{job_id}"


class HhSyncRequest(BaseModel):
    window: str | None = None
    date_from: str | None = None
    date_to: str | None = None


class HhSyncResponse(BaseModel):
    """Итог sync (``HhSyncResult``); у упавшего sync — частичный, на момент сбоя."""

    searched: int
    listings_upserted: int
    unique: int
    duplicates: int
    details_fetched: int
    details_skipped: int
    vacancies_inserted: int
    vacancies_updated: int
    listings_purged: int = 0
    pages: int = 0
    flush_batches: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duration_seconds: float
    window: str = "all"
    period: int | None = None
    date_from: str | None = None
    date_to: str | None = None


class HhSyncProgress(BaseModel):
    phase: str  # search | details | purge
    done: int = 0
    total: int = 0
    searched: int = 0
    pages: int = 0
    unique: int = 0
    duplicates: int = 0
    details_fetched: int = 0
    details_skipped: int = 0
    vacancies_inserted: int = 0
    vacancies_updated: int = 0
    errors: int = 0


class HhSyncAccepted(BaseModel):
    job_id: uuid.UUID
    status: HhSyncJobStatus


class HhSyncJobOut(BaseModel):
    id: uuid.UUID
    status: HhSyncJobStatus
    window: str = "all"
    period: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    progress: HhSyncProgress | None = None
    result: HhSyncResponse | None = None
    error_message: str | None = None
    attempts: int = 0
    # Запрошена отмена; у running — воркер остановит sync перед следующей страницей / группой.
    cancel_requested: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class HhStatusResponse(BaseModel):
    enabled: bool
    token_configured: bool
    email_configured: bool
    listing_ttl_days: int
    search_text: str
    professional_role: list[str]
    listings_total: int
    listings_with_detail: int
    listings_without_detail: int
