"""Репозиторий per-user контента: users, filters, resumes, matches, letters, адаптации, подготовка."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from backend.db.llm_job_repository import refund_queued_llm_jobs_of_resume
from backend.db.models import (
    CoverLetterRow,
    InterviewPrepRow,
    ResumeRow,
    ResumeTailoringRow,
    SavedFilterRow,
    UserRow,
    VacancyMatchRow,
)


def get_user(session: Session, user_id: int) -> UserRow | None:
    return session.get(UserRow, user_id)


def get_user_by_email(session: Session, email: str) -> UserRow | None:
    return session.scalar(select(UserRow).where(UserRow.email == email.lower()))


def count_users(session: Session) -> int:
    return int(session.scalar(select(func.count(UserRow.id))) or 0)


# Ключ pg_advisory_xact_lock регистрации (у HH-сервиса — HH_SYNC_ADVISORY_LOCK_KEY = 874_291_001).
_REGISTRATION_LOCK_KEY = 874_291_002


def lock_user_registration(session: Session) -> None:
    """Сериализовать регистрации до конца транзакции.

    Без этого две параллельные регистрации при пустой ``users`` обе видят
    ``count_users == 0``: два admin'а, а в закрытом режиме — два «первых».
    Advisory lock держится до commit/rollback.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _REGISTRATION_LOCK_KEY})


def create_user(session: Session, *, email: str, password_hash: str, is_admin: bool = False) -> UserRow:
    row = UserRow(
        email=email.lower(),
        password_hash=password_hash,
        is_admin=is_admin,
    )
    session.add(row)
    session.flush()
    return row


def claim_legacy_rows(session: Session, user_id: int) -> None:
    """Привязать ещё ничьи строки (user_id IS NULL) к пользователю — backfill миграции 007."""
    for model in (ResumeRow, SavedFilterRow):
        session.execute(update(model).where(model.user_id.is_(None)).values(user_id=user_id))


def list_saved_filters(session: Session, user_id: int | None = None) -> list[SavedFilterRow]:
    """user_id=None — без скоупа (тесты/служебные вызовы); роуты всегда передают id."""
    stmt = select(SavedFilterRow).order_by(SavedFilterRow.name.asc())
    if user_id is not None:
        stmt = stmt.where(SavedFilterRow.user_id == user_id)
    return list(session.scalars(stmt))


def create_saved_filter(session: Session, *, name: str, params: dict, user_id: int | None = None) -> SavedFilterRow:
    row = SavedFilterRow(name=name, params=params, user_id=user_id)
    session.add(row)
    session.flush()
    return row


def delete_saved_filter(session: Session, filter_id: int, user_id: int | None = None) -> bool:
    row = session.get(SavedFilterRow, filter_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        return False
    session.delete(row)
    session.flush()
    return True


def list_resumes(session: Session, user_id: int | None = None) -> list[ResumeRow]:
    stmt = select(ResumeRow).order_by(ResumeRow.created_at.desc(), ResumeRow.id.desc())
    if user_id is not None:
        stmt = stmt.where(ResumeRow.user_id == user_id)
    return list(session.scalars(stmt))


def get_resume(session: Session, resume_id: int) -> ResumeRow | None:
    return session.get(ResumeRow, resume_id)


def create_resume(
    session: Session,
    *,
    name: str,
    text: str,
    user_id: int | None = None,
) -> ResumeRow:
    row = ResumeRow(name=name, text=text, user_id=user_id)
    session.add(row)
    session.flush()
    return row


def update_resume(
    session: Session,
    resume_id: int,
    *,
    user_id: int | None = None,
    name: str | None = None,
    text: str | None = None,
) -> ResumeRow | None:
    row = session.get(ResumeRow, resume_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        return None
    if name is not None:
        row.name = name
    if text is not None and text != row.text:
        row.text = text
        # Матчи, письма, адаптации и подготовка к собеседованию — кэш по старому тексту: без сброса POST match вернёт его с cached=true.
        _delete_resume_llm_results(session, resume_id)
    session.flush()
    return row


def _delete_resume_llm_results(session: Session, resume_id: int) -> None:
    session.query(VacancyMatchRow).filter(VacancyMatchRow.resume_id == resume_id).delete()
    session.query(CoverLetterRow).filter(CoverLetterRow.resume_id == resume_id).delete()
    session.query(ResumeTailoringRow).filter(ResumeTailoringRow.resume_id == resume_id).delete()
    session.query(InterviewPrepRow).filter(InterviewPrepRow.resume_id == resume_id).delete()


def delete_resume(session: Session, resume_id: int, user_id: int | None = None) -> bool:
    row = session.get(ResumeRow, resume_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        return False
    # Задачи в очереди уйдут каскадом до вызова модели — их вызовы возвращаются в квоту.
    # По задачам в работе модель уже вызвана: их вызов не возвращается.
    refund_queued_llm_jobs_of_resume(session, resume_id)
    _delete_resume_llm_results(session, resume_id)
    session.delete(row)
    session.flush()
    return True


def get_vacancy_match(session: Session, *, resume_id: int, vacancy_id: str) -> VacancyMatchRow | None:
    return session.get(VacancyMatchRow, (resume_id, vacancy_id))


def upsert_vacancy_match(
    session: Session,
    *,
    resume_id: int,
    vacancy_id: str,
    overall_score: int,
    confidence: int,
    breakdown: dict | None,
    explanation: str | None,
    matching_skills: list | None,
    missing_skills: list | None,
    strengths: list | None,
    risks: list | None,
    model: str,
    requested_model: str | None = None,
    prompt_version: str | None = None,
) -> VacancyMatchRow:
    """Записать/обновить оценку пары (resume, vacancy) — кэш матчинга по PK.

    ``model`` — фактически ответившая модель, ``requested_model`` и ``prompt_version`` —
    настроенная модель и версия промпта на момент запроса (по ним считается ``stale``).
    """
    row = session.get(VacancyMatchRow, (resume_id, vacancy_id))
    if row is None:
        row = VacancyMatchRow(
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            overall_score=overall_score,
            confidence=confidence,
            breakdown=breakdown,
            explanation=explanation,
            matching_skills=matching_skills,
            missing_skills=missing_skills,
            strengths=strengths,
            risks=risks,
            model=model,
            requested_model=requested_model,
            prompt_version=prompt_version,
        )
        session.add(row)
    else:
        row.overall_score = overall_score
        row.confidence = confidence
        row.breakdown = breakdown
        row.explanation = explanation
        row.matching_skills = matching_skills
        row.missing_skills = missing_skills
        row.strengths = strengths
        row.risks = risks
        row.model = model
        row.requested_model = requested_model
        row.prompt_version = prompt_version
        row.created_at = datetime.now(UTC)
    session.flush()
    return row


def get_cover_letter(session: Session, *, resume_id: int, vacancy_id: str) -> CoverLetterRow | None:
    return session.get(CoverLetterRow, (resume_id, vacancy_id))


def upsert_cover_letter(
    session: Session,
    *,
    resume_id: int,
    vacancy_id: str,
    text: str,
    tone: str,
    model: str,
    requested_model: str | None = None,
    prompt_version: str | None = None,
) -> CoverLetterRow:
    """Записать/обновить письмо пары (resume, vacancy) — кэш генерации по PK (см. оценку)."""
    row = session.get(CoverLetterRow, (resume_id, vacancy_id))
    if row is None:
        row = CoverLetterRow(
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            text=text,
            tone=tone,
            model=model,
            requested_model=requested_model,
            prompt_version=prompt_version,
        )
        session.add(row)
    else:
        row.text = text
        row.tone = tone
        row.model = model
        row.requested_model = requested_model
        row.prompt_version = prompt_version
        row.created_at = datetime.now(UTC)
    session.flush()
    return row


def delete_cover_letter(session: Session, *, resume_id: int, vacancy_id: str) -> bool:
    row = session.get(CoverLetterRow, (resume_id, vacancy_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def get_resume_tailoring(session: Session, *, resume_id: int, vacancy_id: str) -> ResumeTailoringRow | None:
    return session.get(ResumeTailoringRow, (resume_id, vacancy_id))


def upsert_resume_tailoring(
    session: Session,
    *,
    resume_id: int,
    vacancy_id: str,
    summary: str,
    suggestions: list,
    keywords: list,
    do_not_claim: list,
    model: str,
    requested_model: str | None = None,
    prompt_version: str | None = None,
) -> ResumeTailoringRow:
    """Записать/обновить адаптацию пары (resume, vacancy) — кэш генерации по PK."""
    row = session.get(ResumeTailoringRow, (resume_id, vacancy_id))
    if row is None:
        row = ResumeTailoringRow(resume_id=resume_id, vacancy_id=vacancy_id)
        session.add(row)
    row.summary = summary
    row.suggestions = suggestions
    row.keywords = keywords
    row.do_not_claim = do_not_claim
    row.model = model
    row.requested_model = requested_model
    row.prompt_version = prompt_version
    row.created_at = datetime.now(UTC)
    session.flush()
    return row


def delete_resume_tailoring(session: Session, *, resume_id: int, vacancy_id: str) -> bool:
    row = session.get(ResumeTailoringRow, (resume_id, vacancy_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def get_interview_prep(session: Session, *, resume_id: int, vacancy_id: str) -> InterviewPrepRow | None:
    return session.get(InterviewPrepRow, (resume_id, vacancy_id))


def upsert_interview_prep(
    session: Session,
    *,
    resume_id: int,
    vacancy_id: str,
    summary: str,
    questions: list,
    gaps: list,
    questions_to_ask: list,
    model: str,
    requested_model: str | None = None,
    prompt_version: str | None = None,
) -> InterviewPrepRow:
    """Записать/обновить подготовку пары (resume, vacancy) — кэш генерации по PK."""
    row = session.get(InterviewPrepRow, (resume_id, vacancy_id))
    if row is None:
        row = InterviewPrepRow(resume_id=resume_id, vacancy_id=vacancy_id)
        session.add(row)
    row.summary = summary
    row.questions = questions
    row.gaps = gaps
    row.questions_to_ask = questions_to_ask
    row.model = model
    row.requested_model = requested_model
    row.prompt_version = prompt_version
    row.created_at = datetime.now(UTC)
    session.flush()
    return row


def delete_interview_prep(session: Session, *, resume_id: int, vacancy_id: str) -> bool:
    row = session.get(InterviewPrepRow, (resume_id, vacancy_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
