"""Периодическая очистка служебных таблиц (вызывается из hourly-потока backend).

Без неё растут: просроченный кэш LLM (уходил только при чтении того же ключа) и кэш
от прежних версий промпта, старые курсы валют,
завершённые LLM-джобы, ключи идемпотентности и журнал прогонов. Удаление — пачками
в отдельных транзакциях, чтобы не держать длинные блокировки. Историю HH sync чистит
HH-сервис (``jobdesk_hh.maintenance``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from backend.db import ai_cache_repository as ai_cache_repo
from backend.db import fx_repository as fx_repo
from backend.db import idempotency_repository as idem_repo
from backend.db import llm_job_repository as llm_repo
from backend.db import parse_run_repository as run_repo
from backend.db import refresh_token_repository as refresh_repo
from backend.db.session import session_scope
from jobdesk.ai.prompts import EXTRACT_PROMPT_VERSION
from jobdesk.config.schema import RetentionConfig

_LOG = logging.getLogger(__name__)

_BATCH = 1000
# Потолок за один проход: остаток догонит следующий час.
_MAX_BATCHES = 50


@dataclass
class CleanupStats:
    ai_cache: int = 0
    llm_jobs: int = 0
    idempotency_keys: int = 0
    parse_runs: int = 0
    refresh_tokens: int = 0
    exchange_rates: int = 0

    @property
    def total(self) -> int:
        return (
            self.ai_cache
            + self.llm_jobs
            + self.idempotency_keys
            + self.parse_runs
            + self.refresh_tokens
            + self.exchange_rates
        )


def _purge_in_batches(purge: Callable[[Session, int], int], *, batch: int = _BATCH) -> int:
    total = 0
    for _ in range(_MAX_BATCHES):
        with session_scope() as session:
            n = purge(session, batch)
        total += n
        if n < batch:
            break
    return total


def purge_service_tables(
    retention: RetentionConfig,
    *,
    fx_sync_days: int = 0,
    now: datetime | None = None,
) -> CleanupStats:
    """Один проход очистки. Ошибка одной таблицы не мешает остальным.

    ``fx_sync_days`` — ``fx.sync_days_on_startup``: курсы из этого окна не удаляются,
    иначе каждый старт backend заново качал бы их из ЦБ.
    """
    now = now or datetime.now(UTC)
    stats = CleanupStats()

    steps: list[tuple[str, Callable[[Session, int], int]]] = [
        (
            "ai_cache",
            lambda s, n: ai_cache_repo.purge_stale_cache(s, current_prompt_version=EXTRACT_PROMPT_VERSION, limit=n),
        ),
        ("idempotency_keys", lambda s, n: idem_repo.purge_expired_idempotency(s, limit=n)),
        ("refresh_tokens", lambda s, n: refresh_repo.purge_refresh_tokens(s, now=now, limit=n)),
    ]
    if retention.llm_jobs_ttl_days > 0:
        llm_cutoff = now - timedelta(days=retention.llm_jobs_ttl_days)
        steps.append(("llm_jobs", lambda s, n: llm_repo.purge_finished_llm_jobs(s, older_than=llm_cutoff, limit=n)))
    if retention.parse_runs_ttl_days > 0:
        runs_cutoff = now - timedelta(days=retention.parse_runs_ttl_days)
        steps.append(("parse_runs", lambda s, n: run_repo.purge_old_parse_runs(s, older_than=runs_cutoff, limit=n)))

    if retention.exchange_rates_ttl_days > 0:
        fx_cutoff = now.date() - timedelta(days=max(retention.exchange_rates_ttl_days, fx_sync_days))
        steps.append(("exchange_rates", lambda s, n: fx_repo.purge_old_rates(s, older_than=fx_cutoff, limit=n)))

    for name, purge in steps:
        try:
            setattr(stats, name, _purge_in_batches(purge))
        except Exception as e:
            _LOG.warning("Очистка %s не выполнена: %s", name, e)
    if stats.total:
        _LOG.info("Очистка служебных таблиц: %s", asdict(stats))
    return stats
