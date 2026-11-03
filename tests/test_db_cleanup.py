"""Периодическая очистка служебных таблиц: кэш LLM, LLM-джобы, idempotency, прогоны."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import new_user

from backend.db import vacancy_repository
from backend.db.models import (
    AIExtractionCacheRow,
    ExchangeRateRow,
    IdempotencyKeyRow,
    LlmJobRow,
    ParseRunRow,
)
from backend.db.session import session_scope
from backend.services import db_cleanup
from backend.services.db_cleanup import purge_service_tables
from jobdesk.ai.prompts import EXTRACT_PROMPT_VERSION
from jobdesk.config.schema import RetentionConfig

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _db(pg_db):
    with session_scope() as session:
        new_user(session)  # id=1 — владелец джоб из _job()


def _cache(key: str, expires_at: datetime | None, prompt_version: str = EXTRACT_PROMPT_VERSION) -> AIExtractionCacheRow:
    return AIExtractionCacheRow(
        cache_key=key, model="m", prompt_version=prompt_version, mode="primary", payload={}, expires_at=expires_at
    )


def _job(status: str, finished_days_ago: float | None, created_days_ago: float = 100) -> LlmJobRow:
    return LlmJobRow(
        user_id=1,
        kind="import",
        status=status,
        created_at=NOW - timedelta(days=created_days_ago),
        finished_at=None if finished_days_ago is None else NOW - timedelta(days=finished_days_ago),
    )


def _run(status: str, finished_days_ago: float | None, created_days_ago: float = 400) -> ParseRunRow:
    return ParseRunRow(
        status=status,
        config_snapshot={},
        created_at=NOW - timedelta(days=created_days_ago),
        finished_at=None if finished_days_ago is None else NOW - timedelta(days=finished_days_ago),
    )


def _count(model) -> int:
    with session_scope() as session:
        return int(session.scalar(select(func.count()).select_from(model)))


def test_purges_only_expired_and_finished_rows():
    real_now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                _cache("expired", real_now - timedelta(days=1)),
                _cache("fresh", real_now + timedelta(days=1)),
                _cache("forever", None),
                _cache("old-prompt", real_now + timedelta(days=1), prompt_version="extract-v0"),
                _job("done", finished_days_ago=40),
                _job("error", finished_days_ago=None, created_days_ago=40),  # без finished_at — по created_at
                _job("done", finished_days_ago=5),
                _job("running", finished_days_ago=None, created_days_ago=40),  # активные не трогаем
                IdempotencyKeyRow(
                    key="old", scope="s", actor="a", status_code=200, body={}, expires_at=real_now - timedelta(hours=1)
                ),
                IdempotencyKeyRow(
                    key="new", scope="s", actor="a", status_code=200, body={}, expires_at=real_now + timedelta(hours=1)
                ),
                _run("completed", finished_days_ago=200),
                _run("failed", finished_days_ago=None, created_days_ago=200),
                _run("completed", finished_days_ago=10),
                _run("running", finished_days_ago=None, created_days_ago=200),
            ]
        )

    stats = purge_service_tables(RetentionConfig(llm_jobs_ttl_days=30, parse_runs_ttl_days=180), now=NOW)

    assert (stats.ai_cache, stats.llm_jobs, stats.idempotency_keys, stats.parse_runs) == (2, 2, 1, 2)
    with session_scope() as session:
        assert set(session.scalars(select(AIExtractionCacheRow.cache_key))) == {"fresh", "forever"}
        assert sorted(session.scalars(select(LlmJobRow.status))) == ["done", "running"]
        assert set(session.scalars(select(IdempotencyKeyRow.key))) == {"new"}
        assert sorted(session.scalars(select(ParseRunRow.status))) == ["completed", "running"]


def test_zero_ttl_keeps_jobs_and_runs():
    with session_scope() as session:
        session.add_all([_job("done", finished_days_ago=1000), _run("completed", finished_days_ago=1000)])

    stats = purge_service_tables(RetentionConfig(llm_jobs_ttl_days=0, parse_runs_ttl_days=0), now=NOW)

    assert stats.llm_jobs == stats.parse_runs == 0
    assert _count(LlmJobRow) == 1
    assert _count(ParseRunRow) == 1


def test_purge_runs_in_batches(monkeypatch):
    monkeypatch.setattr(db_cleanup, "_BATCH", 2)
    with session_scope() as session:
        session.add_all([_run("completed", finished_days_ago=365) for _ in range(5)])

    stats = purge_service_tables(RetentionConfig(parse_runs_ttl_days=180), now=NOW)

    assert stats.parse_runs == 5
    assert _count(ParseRunRow) == 0


def test_failing_table_does_not_stop_others(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_cleanup.ai_cache_repo, "purge_stale_cache", boom)
    with session_scope() as session:
        session.add(_run("completed", finished_days_ago=365))

    stats = purge_service_tables(RetentionConfig(parse_runs_ttl_days=180), now=NOW)

    assert stats.ai_cache == 0
    assert stats.parse_runs == 1


def test_count_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(vacancy_repository, "_COUNT_CACHE_MAX_ENTRIES", 4)
    vacancy_repository.invalidate_vacancy_count_cache()
    for i in range(10):
        vacancy_repository._set_cached_vacancy_count(f"q={uuid.uuid4()}-{i}", i)
        assert len(vacancy_repository._count_cache) <= 4
    vacancy_repository.invalidate_vacancy_count_cache()


def _rate(days_ago: int, currency: str = "USD") -> ExchangeRateRow:
    return ExchangeRateRow(rate_date=NOW.date() - timedelta(days=days_ago), currency=currency, rate_to_rub=90.0)


def _rate_ages() -> list[int]:
    with session_scope() as session:
        dates = session.scalars(select(ExchangeRateRow.rate_date).distinct())
        return sorted((NOW.date() - d).days for d in dates)


def test_purges_old_exchange_rates():
    with session_scope() as session:
        session.add_all([_rate(0), _rate(0, "EUR"), _rate(59), _rate(61), _rate(61, "EUR"), _rate(200)])

    stats = purge_service_tables(RetentionConfig(exchange_rates_ttl_days=60), now=NOW)

    assert stats.exchange_rates == 3
    assert _rate_ages() == [0, 59]


def test_exchange_rates_keep_latest_date_and_startup_sync_window():
    with session_scope() as session:
        session.add_all([_rate(100), _rate(100, "EUR"), _rate(150)])  # ЦБ давно недоступен

    stats = purge_service_tables(RetentionConfig(exchange_rates_ttl_days=60), now=NOW)
    assert stats.exchange_rates == 1
    assert _rate_ages() == [100]  # последняя дата — единственный источник курса

    with session_scope() as session:
        session.add_all([_rate(0), _rate(80)])
    stats = purge_service_tables(RetentionConfig(exchange_rates_ttl_days=60), fx_sync_days=90, now=NOW)
    assert stats.exchange_rates == 2  # 100 дней > окна 90; 80 — внутри окна догрузки
    assert _rate_ages() == [0, 80]


def test_exchange_rates_zero_ttl_keeps_all():
    with session_scope() as session:
        session.add_all([_rate(0), _rate(1000)])

    stats = purge_service_tables(RetentionConfig(exchange_rates_ttl_days=0), now=NOW)

    assert stats.exchange_rates == 0
    assert _rate_ages() == [0, 1000]
