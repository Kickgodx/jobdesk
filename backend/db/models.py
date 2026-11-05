"""SQLAlchemy-модели PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.vector_type import Vector


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ParseRunRow(Base):
    __tablename__ = "parse_runs"
    __table_args__ = (
        # Один active parse: partial UNIQUE по выражению ((true)). См. миграцию 015.
        Index(
            "uq_parse_runs_one_active",
            text("(true)"),
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    run_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    posts_fetched: Mapped[int] = mapped_column(Integer, default=0)
    parse_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Сколько раз прогон брали в работу (claim); лимит — PARSE_JOB_MAX_ATTEMPTS.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ExchangeRateRow(Base):
    """Курс валюты к рублю на дату (источник — ЦБ РФ)."""

    __tablename__ = "exchange_rates"

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    rate_to_rub: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="cbr")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def writable_columns(model: type[Base]) -> list:
    """Колонки, которые пишет приложение: generated-колонки (``Computed``) считает Postgres.

    INSERT/UPDATE со значением в generated-колонке Postgres отклоняет, а чтение
    deferred-колонки по одной строке — лишний запрос на каждую.
    """
    return [c for c in model.__table__.columns if c.computed is None]


class VacancyRow(Base):
    """Уникальная вакансия по тексту (``id`` = sha256 normalize(cleaned_text))."""

    __tablename__ = "vacancies"
    __table_args__ = (
        # Список по умолчанию: WHERE NOT duplicate AND archived IS NULL
        # ORDER BY post_date DESC NULLS LAST, id — как в миграции 013.
        Index(
            "ix_vacancies_active_post_date",
            text("post_date DESC NULLS LAST"),
            text("id"),
            postgresql_where=text("is_duplicate IS false AND archived_at IS NULL"),
        ),
        # Сортировки «день ↓ + зарплата ₽ ↓» и «зарплата ₽ ↓ + дата ↓» — миграция 034.
        Index(
            "ix_vacancies_active_day_salary",
            text("((post_date AT TIME ZONE 'UTC')::date) DESC NULLS LAST"),
            text("detected_salary_avg_rub DESC NULLS LAST"),
            text("post_date DESC NULLS LAST"),
            text("id"),
            postgresql_where=text("is_duplicate IS false AND archived_at IS NULL"),
        ),
        Index(
            "ix_vacancies_active_salary_date",
            text("detected_salary_avg_rub DESC NULLS LAST"),
            text("post_date DESC NULLS LAST"),
            text("id"),
            postgresql_where=text("is_duplicate IS false AND archived_at IS NULL"),
        ),
    )
    # Без RETURNING серверных значений после INSERT: единственное такое — search_tsv,
    # он deferred и вычитывать его на каждую вставку незачем.
    __mapper_args__: ClassVar[dict] = {"eager_defaults": False}

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_post_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_channel: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, nullable=False)

    # JSONB + GIN (миграция 019): фильтр по специализации идёт по индексу.
    detected_specializations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    detected_grade: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    detected_salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    detected_salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    detected_salary_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    detected_salary_avg_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)

    work_format: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(256), nullable=True)
    city: Mapped[str | None] = mapped_column(String(256), nullable=True)
    company: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Нормализованное имя компании (``jobdesk.utils.company.company_key``, миграция 040):
    # сводит «ООО Ромашка» и «РОМАШКА» в профиле компании и топе работодателей.
    company_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    contact: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # JSONB + GIN (миграция 037): фильтр по технологии идёт по индексу.
    technologies: Mapped[list[str]] = mapped_column(JSONB, default=list)
    required_experience: Mapped[str | None] = mapped_column(Text, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    application_urls: Mapped[list[str]] = mapped_column(JSON, default=list)

    extraction_source: Mapped[str] = mapped_column(String(16), default="rules")
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    vacancy_score: Mapped[int] = mapped_column(Integer, default=0)
    vacancy_substantive_score: Mapped[int] = mapped_column(Integer, default=0)

    is_duplicate: Mapped[bool] = mapped_column(default=False)
    duplicate_group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duplicate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Скрытая копия (``db merge-duplicates``) → исходная вакансия; у уникальных — NULL.
    duplicate_of: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="SET NULL", name="fk_vacancies_duplicate_of"),
        nullable=True,
        index=True,
    )

    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    last_parse_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Документ полнотекстового поиска (миграция 033, GIN ix_vacancies_search_tsv). Считает Postgres:
    # ORM колонку не пишет, а deferred — не тянет в каждый SELECT сущности.
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('russian', cleaned_text)", persisted=True),
        deferred=True,
    )


class ParseRunVacancyRow(Base):
    """Какие вакансии встретил прогон.

    ``vacancies.last_parse_run_id`` хранит только последний прогон и перезаписывается,
    а по этой связке история и экспорт прогона не пустеют после следующих прогонов.
    """

    __tablename__ = "parse_run_vacancies"

    parse_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parse_runs.id", ondelete="CASCADE"), primary_key=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class VacancySightingRow(Base):
    """Появление вакансии в посте Telegram: один пост — одна строка.

    В ``vacancies`` попадает только первый пост с данным текстом. Дубли (тот же
    или похожий текст в другом канале, перепост в том же канале, повтор из прошлых
    прогонов) пишутся сюда со ссылкой на каноническую вакансию: по ним считается
    доля уникального контента канала.
    """

    __tablename__ = "vacancy_sightings"

    # {channel}-{post_id}: повторная вычитка того же поста не создаёт второе появление.
    source_post_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    vacancy_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_channel: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    post_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SET NULL: очистка старых прогонов не должна стирать историю появлений.
    parse_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parse_runs.id", ondelete="SET NULL"), nullable=True
    )
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # exact_raw_hash | normalized_hash | fuzzy:<score> | seen_before; NULL — оригинал.
    duplicate_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Перепост с правками: отличия от первой публикации (``deduplication.changes``); иначе NULL.
    # none_as_null: «изменений нет» — SQL NULL, а не JSON null.
    changes: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class UserRow(Base):
    """Пользователь: владелец резюме, пресетов фильтров и (косвенно) матчей."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Версия выданных токенов (claim ``ver``): +1 при смене пароля и «выйти везде»
    # отзывает все ранее выданные JWT пользователя.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class RefreshTokenRow(Base):
    """Refresh-токен сессии (httpOnly cookie): хранится только sha256 значения.

    Каждый ``/auth/refresh`` ротирует токен: строка помечается ``rotated_at``, в той же
    семье (``family_id`` — один вход) выдаётся новая. Повтор ротированного токена после
    grace-окна — признак кражи: отзывается вся семья.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # users.token_version на момент выдачи: смена пароля и «выйти везде» отзывают и refresh.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiTokenRow(Base):
    """Персональный токен доступа (PAT, ``tgj_…``): хранится только sha256 значения.

    ``scopes`` — что разрешено (``read`` — GET, ``llm`` — постановка LLM-задач); admin-эндпоинты
    PAT не открывает. ``token_version`` — как у refresh: смена пароля и «выйти везде» отзывают и PAT.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SavedFilterRow(Base):
    """Именованный набор фильтров списка вакансий (JSON — поля VacancyQuery)."""

    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class ResumeRow(Base):
    """Резюме кандидата для матчинга с вакансиями."""

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class VacancyMatchRow(Base):
    """Оценка соответствия резюме вакансии. PK (resume+vacancy) — и кэш матчинга; одна строка на пару."""

    __tablename__ = "vacancy_matches"

    resume_id: Mapped[int] = mapped_column(Integer, ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True)
    vacancy_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True
    )
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    matching_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    strengths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    risks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Фактически ответившая модель (может быть fallback).
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Настроенная модель матчинга и версия промпта на момент запроса — по ним
    # ``backend.services.llm_result_freshness`` решает, устарела ли оценка.
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class CoverLetterRow(Base):
    """Сгенерированное сопроводительное письмо. PK (resume+vacancy) — и кэш генерации; одна строка на пару."""

    __tablename__ = "cover_letters"

    resume_id: Mapped[int] = mapped_column(Integer, ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True)
    vacancy_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str] = mapped_column(String(32), nullable=False)
    # Фактически ответившая модель (может быть fallback).
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Настроенная модель писем и версия промпта на момент запроса (см. оценку выше).
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class ResumeTailoringRow(Base):
    """Адаптация резюме под вакансию (LLM). PK (resume+vacancy) — и кэш генерации, как у писем."""

    __tablename__ = "resume_tailorings"

    resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_tailorings_resume_id"), primary_key=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="CASCADE", name="fk_resume_tailorings_vacancy_id"),
        primary_key=True,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    suggestions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    do_not_claim: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Фактически ответившая модель (может быть fallback); запрошенная и версия промпта — для stale.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class VacancyEmbeddingRow(Base):
    """Эмбеддинг вакансии (миграция 042, HNSW по косинусу). ``content_hash`` — от текста и модели."""

    __tablename__ = "vacancy_embeddings"

    vacancy_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="CASCADE", name="fk_vacancy_embeddings_vacancy_id"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class VacancySalaryEstimateRow(Base):
    """Ориентировочная зарплата вакансии без вилки (миграция 050): квантили ближайших по смыслу.

    Пересчитывает лидер API (``salary_estimate_service.sweep_estimates``); в аналитику не входит.
    ``median`` NULL — похожих с зарплатой не набралось.
    """

    __tablename__ = "vacancy_salary_estimates"

    vacancy_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="CASCADE", name="fk_vacancy_salary_estimates_vacancy_id"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Грейд, внутри которого искали похожих; NULL — грейд вакансии неизвестен, искали среди всех.
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    neighbours: Mapped[int] = mapped_column(Integer, nullable=False)
    p25: Mapped[float | None] = mapped_column(Float, nullable=True)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    p75: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Похожие вакансии, ближайшие первыми.
    neighbour_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class ResumeEmbeddingRow(Base):
    """Эмбеддинг резюме для подбора вакансий; пересчитывается при смене текста или модели."""

    __tablename__ = "resume_embeddings"

    resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_embeddings_resume_id"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class InterviewPrepRow(Base):
    """Подготовка к собеседованию (LLM). PK (resume+vacancy) — и кэш генерации, как у адаптаций."""

    __tablename__ = "interview_preps"

    resume_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resumes.id", ondelete="CASCADE", name="fk_interview_preps_resume_id"), primary_key=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="CASCADE", name="fk_interview_preps_vacancy_id"),
        primary_key=True,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    gaps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    questions_to_ask: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Фактически ответившая модель (может быть fallback); запрошенная и версия промпта — для stale.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class MarketDigestRow(Base):
    """Еженедельный обзор рынка: факты недели (JSON) и текст LLM по ним; одна строка на неделю."""

    __tablename__ = "market_digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    facts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())

    __table_args__ = (UniqueConstraint("period_start", name="uq_market_digests_period_start"),)


class MarketMapRow(Base):
    """Расчёт карты рынка (миграция 048): параметры и кластеры; хранится только последний."""

    __tablename__ = "market_maps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    clusters: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class MaintenanceRequestRow(Base):
    """Запрос фоновой работы вне расписания (миграция 051): одна строка на задачу."""

    __tablename__ = "maintenance_requests"

    task: Mapped[str] = mapped_column(String(32), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class MarketMapPointRow(Base):
    """Вакансия на карте рынка: 2D-координаты (0–1) и номер кластера."""

    __tablename__ = "market_map_points"

    map_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("market_maps.id", ondelete="CASCADE", name="fk_market_map_points_map_id"), primary_key=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("vacancies.id", ondelete="CASCADE", name="fk_market_map_points_vacancy_id"),
        primary_key=True,
        index=True,
    )
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    cluster: Mapped[int] = mapped_column(Integer, nullable=False)


class LlmJobRow(Base):
    """Фоновая LLM-задача (мэтчинг / сопроводительное письмо / ручной импорт)."""

    __tablename__ = "llm_jobs"
    __table_args__ = (
        # Одна активная джоба на пару резюме–вакансия (миграция 028); импорт — без пары.
        Index(
            "uq_llm_jobs_one_active_pair",
            "user_id",
            "kind",
            "resume_id",
            "vacancy_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running') AND kind <> 'import'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # match | cover_letter | resume_tailor | interview_prep | import | reextract | match_batch
    # Индексы на FK (миграция 018): каскадное удаление резюме / вакансии без seq scan.
    # У импорта (миграция 023) их нет — тексты в params.
    resume_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("resumes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    vacancy_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    force: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Импорт: поэлементный итог, дописывается по мере обработки (ретрай продолжает с него).
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Сколько раз джобу брали в работу (claim); лимит — LLM_JOB_MAX_ATTEMPTS.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Запрошена отмена (миграция 031): queued отменяется сразу, running — воркером.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # UTC-сутки списания вызова квоты (миграция 032); пусто — не списан или уже возвращён.
    quota_day: Mapped[date | None] = mapped_column(Date, nullable=True)


class IdempotencyKeyRow(Base):
    """Кэш ответов мутаций по Idempotency-Key (TTL)."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), primary_key=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class AIExtractionCacheRow(Base):
    """Кэш ответов LLM (ключ = sha256 текста + model + prompt_version + mode)."""

    __tablename__ = "ai_extraction_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class TelegramChannelCheckpointRow(Base):
    """Чекпоинт инкрементальной вычитки канала: прочитано всё с ``covered_from`` по ``last_message_id``.

    Пишется сразу, как только посты канала разобраны и записаны в БД, пока ни один
    пост прогона не потерян из-за LLM (``checkpoint_sink`` в ``run_parse``).
    """

    __tablename__ = "telegram_channel_checkpoints"

    channel: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    covered_from: Mapped[date] = mapped_column(Date, nullable=False)
    parse_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ChannelCandidateRow(Base):
    """Канал, из которого пересылают посты в наши каналы: кандидат в источники."""

    __tablename__ = "channel_candidates"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Последние увиденные: канал может сменить username или название.
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )  # new | ignored
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class TelegramForwardRow(Base):
    """Пересланный пост нашего канала. PK — канал + id поста: перечитанное окно не удваивает счётчики."""

    __tablename__ = "telegram_forwards"

    channel: Mapped[str] = mapped_column(String(128), primary_key=True)
    post_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("channel_candidates.channel_id", ondelete="CASCADE", name="fk_telegram_forwards_source_channel_id"),
        nullable=False,
        index=True,
    )
    source_post_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    post_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_vacancy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class LlmUserUsageRow(Base):
    """Вызовы LLM пользователя за сутки (UTC) — для квоты ``LLM_USER_DAILY_CALLS``."""

    __tablename__ = "llm_user_usage"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
