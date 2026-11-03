from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.api.deps import CurrentUser, get_current_user, get_db, pat_scope, verify_api_key
from backend.db import repository as repo
from backend.db.models import CoverLetterRow, InterviewPrepRow, ResumeRow, ResumeTailoringRow, VacancyMatchRow
from backend.observability import record_cover_letter, record_match
from backend.schemas import (
    RESUME_TEXT_MAX_LENGTH,
    CareerPathsOut,
    CoverLetterOut,
    CoverLetterRequest,
    InterviewPrepOut,
    LlmJobAccepted,
    MatchBatchRequest,
    ResumeCreate,
    ResumeFileTextOut,
    ResumeListResponse,
    ResumeMarketOut,
    ResumeOut,
    ResumeTailoringOut,
    ResumeUpdate,
    SkillGapsOut,
    SkillWhatIfOut,
    VacancyMatchOut,
)
from backend.services.llm_job_service import (
    KIND_COVER_LETTER,
    KIND_INTERVIEW_PREP,
    KIND_MATCH,
    KIND_MATCH_BATCH,
    KIND_RESUME_TAILOR,
    LlmDailyQuotaError,
    LlmJobUserLimitError,
    llm_job_runner,
)
from backend.services.llm_result_freshness import (
    cover_letter_is_stale,
    current_ai_config,
    interview_prep_is_stale,
    match_is_stale,
    resume_tailoring_is_stale,
)
from backend.services.resume_market_service import DEFAULT_PEERS, MAX_PEERS, MIN_PEERS, resume_market
from backend.services.skill_gap_service import skill_gaps
from jobdesk.ai.schema import (
    InterviewGap,
    InterviewQuestion,
    MatchBreakdown,
    ResumeTailoringSuggestion,
    SkillMatch,
)
from jobdesk.config.schema import AiConfig
from jobdesk.utils.resume_file import ResumeFileError, extract_resume_text

_LOG = logging.getLogger(__name__)

resumes_router = APIRouter(prefix="/resumes", tags=["resumes"], dependencies=[Depends(verify_api_key)])


def _to_out(row) -> ResumeOut:
    return ResumeOut(id=row.id, name=row.name, text=row.text, created_at=row.created_at)


def _get_own_resume(db: Session, resume_id: int, user: CurrentUser) -> ResumeRow:
    row = repo.get_resume(db, resume_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Resume not found")
    return row


@resumes_router.get("", response_model=ResumeListResponse)
def list_resumes(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeListResponse:
    items = [_to_out(r) for r in repo.list_resumes(db, user_id=user.id)]
    return ResumeListResponse(items=items)


@resumes_router.post("", response_model=ResumeOut, status_code=201)
def create_resume(
    body: ResumeCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeOut:
    row = repo.create_resume(db, name=body.name, text=body.text, user_id=user.id)
    return _to_out(row)


# Резюме в PDF/DOCX редко больше 1–2 МБ; лимит отсекает случайные архивы и сканы.
RESUME_FILE_MAX_BYTES = 5 * 1024 * 1024


@resumes_router.post("/parse-file", response_model=ResumeFileTextOut)
async def parse_resume_file(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> ResumeFileTextOut:
    """Текст резюме из PDF или DOCX: тело запроса — байты файла (без multipart).

    Формат определяется по сигнатуре файла. Текст не сохраняется — UI подставляет его в
    форму резюме для правки. Больше 5 МБ — 413; не PDF/DOCX, пароль, нет текста — 422.
    """
    from backend.errors import CODE_VALIDATION, http_error

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > RESUME_FILE_MAX_BYTES:
        raise http_error(413, CODE_VALIDATION, "Файл больше 5 МБ")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > RESUME_FILE_MAX_BYTES:
            raise http_error(413, CODE_VALIDATION, "Файл больше 5 МБ")
    if not data:
        raise http_error(422, CODE_VALIDATION, "Пустой файл")
    try:
        parsed = await run_in_threadpool(extract_resume_text, bytes(data), max_chars=RESUME_TEXT_MAX_LENGTH)
    except ResumeFileError as e:
        raise http_error(422, CODE_VALIDATION, str(e)) from e
    _LOG.info("Резюме из файла: %s, %s символов, user=%s", parsed.format, len(parsed.text), user.id)
    return ResumeFileTextOut(
        text=parsed.text,
        format=parsed.format,
        chars=len(parsed.text),
        truncated=parsed.truncated,
        pages=parsed.pages,
    )


@resumes_router.get("/{resume_id}", response_model=ResumeOut)
def get_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeOut:
    return _to_out(_get_own_resume(db, resume_id, user))


@resumes_router.put("/{resume_id}", response_model=ResumeOut)
def update_resume(
    resume_id: int,
    body: ResumeUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeOut:
    row = repo.update_resume(db, resume_id, user_id=user.id, name=body.name, text=body.text)
    if row is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return _to_out(row)


@resumes_router.delete("/{resume_id}", status_code=204)
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> None:
    if not repo.delete_resume(db, resume_id, user_id=user.id):
        raise HTTPException(status_code=404, detail="Resume not found")


@resumes_router.get("/{resume_id}/skill-gaps", response_model=SkillGapsOut)
def get_skill_gaps(
    resume_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> SkillGapsOut:
    """Сводка недостающих навыков по всем сохранённым оценкам резюме (без вызова LLM)."""
    _get_own_resume(db, resume_id, user)
    return skill_gaps(db, resume_id=resume_id)


@resumes_router.get("/{resume_id}/market", response_model=ResumeMarketOut)
def get_resume_market(
    resume_id: int,
    peers: int = Query(
        default=DEFAULT_PEERS,
        ge=MIN_PEERS,
        le=MAX_PEERS,
        description="Сколько ближайших вакансий учитывать в профиле и технологиях.",
    ),
    grade: str | None = Query(
        default=None, max_length=32, description="Только вакансии этого грейда (junior, middle, senior, lead…)."
    ),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeMarketOut:
    """Резюме против рынка: вилка, профиль и технологии ближайших по смыслу вакансий (без LLM)."""
    from backend.errors import CODE_UPSTREAM_UNAVAILABLE, CODE_VALIDATION, http_error
    from backend.services.embedding_service import EmbeddingsUnavailable
    from jobdesk.extraction.grade_extractor import UNKNOWN_GRADE, normalize_grade

    resume = _get_own_resume(db, resume_id, user)
    if grade:
        grade = normalize_grade(grade)
        if grade == UNKNOWN_GRADE:
            raise http_error(422, CODE_VALIDATION, "Неизвестный грейд.", retryable=False)
    try:
        return resume_market(db, resume, peers_limit=peers, grade=grade or None)
    except EmbeddingsUnavailable as e:
        raise http_error(503, CODE_UPSTREAM_UNAVAILABLE, f"Сравнение с рынком недоступно: {e}", retryable=True) from e


@resumes_router.get("/{resume_id}/what-if", response_model=SkillWhatIfOut)
def get_skill_what_if(
    resume_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> SkillWhatIfOut:
    """Что если выучить: недостающие технологии и их эффект для ближайших к резюме вакансий (без LLM)."""
    from backend.errors import CODE_UPSTREAM_UNAVAILABLE, http_error
    from backend.services.embedding_service import EmbeddingsUnavailable
    from backend.services.skill_what_if_service import skill_what_if

    resume = _get_own_resume(db, resume_id, user)
    try:
        return skill_what_if(db, resume)
    except EmbeddingsUnavailable as e:
        raise http_error(503, CODE_UPSTREAM_UNAVAILABLE, f"Расчёт недоступен: {e}", retryable=True) from e


@resumes_router.get("/{resume_id}/career-paths", response_model=CareerPathsOut)
def get_career_paths(
    resume_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> CareerPathsOut:
    """Карьерная траектория: следующий грейд и соседние роли с маршрутом технологий (без LLM)."""
    from backend.errors import CODE_UPSTREAM_UNAVAILABLE, http_error
    from backend.services.career_paths_service import career_paths
    from backend.services.embedding_service import EmbeddingsUnavailable

    resume = _get_own_resume(db, resume_id, user)
    try:
        return career_paths(db, resume)
    except EmbeddingsUnavailable as e:
        raise http_error(503, CODE_UPSTREAM_UNAVAILABLE, f"Расчёт недоступен: {e}", retryable=True) from e


def _match_to_out(row: VacancyMatchRow, *, cached: bool, ai: AiConfig) -> VacancyMatchOut:
    return VacancyMatchOut(
        resume_id=row.resume_id,
        vacancy_id=row.vacancy_id,
        overall_score=row.overall_score,
        confidence=row.confidence,
        breakdown=MatchBreakdown.model_validate(row.breakdown or {}),
        explanation=row.explanation,
        matching_skills=[SkillMatch.model_validate(s) for s in row.matching_skills or []],
        missing_skills=[SkillMatch.model_validate(s) for s in row.missing_skills or []],
        strengths=row.strengths or [],
        risks=row.risks or [],
        model=row.model,
        created_at=row.created_at,
        cached=cached,
        stale=match_is_stale(row, ai),
    )


@resumes_router.get("/{resume_id}/match/{vacancy_id}", response_model=VacancyMatchOut)
def get_vacancy_match(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> VacancyMatchOut:
    """Уже посчитанная оценка пары; 404 — если матчинг ещё не запускался."""
    _get_own_resume(db, resume_id, user)
    row = repo.get_vacancy_match(db, resume_id=resume_id, vacancy_id=vacancy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return _match_to_out(row, cached=True, ai=current_ai_config())


@resumes_router.post(
    "/{resume_id}/match/{vacancy_id}",
    response_model=None,
    responses={
        200: {"model": VacancyMatchOut},
        202: {"model": LlmJobAccepted},
    },
)
@pat_scope("llm")
def match_single_vacancy(
    resume_id: int,
    vacancy_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Свежая оценка без force → 200; иначе (нет, устарела, force) фоновая джоба → 202 ``{job_id}``."""
    _get_own_resume(db, resume_id, user)
    vacancy = repo.get_vacancy_row(db, vacancy_id)
    if vacancy is None:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if not force:
        existing = repo.get_vacancy_match(db, resume_id=resume_id, vacancy_id=vacancy_id)
        ai = current_ai_config()
        if existing is not None and not match_is_stale(existing, ai):
            record_match(outcome="cached")
            return _match_to_out(existing, cached=True, ai=ai)
    try:
        result = llm_job_runner.enqueue(
            user_id=user.id,
            kind=KIND_MATCH,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            force=force,
        )
    except LlmJobUserLimitError as e:
        from backend.errors import CODE_RATE_LIMITED, http_error

        raise http_error(429, CODE_RATE_LIMITED, str(e), retryable=True) from e
    except LlmDailyQuotaError as e:
        from backend.errors import CODE_LLM_QUOTA_EXCEEDED, http_error

        raise http_error(429, CODE_LLM_QUOTA_EXCEEDED, str(e), retryable=False) from e
    body = LlmJobAccepted(job_id=result.job_id, status="queued")
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


def _letter_to_out(row: CoverLetterRow, *, cached: bool, ai: AiConfig) -> CoverLetterOut:
    return CoverLetterOut(
        resume_id=row.resume_id,
        vacancy_id=row.vacancy_id,
        text=row.text,
        tone=row.tone,
        model=row.model,
        created_at=row.created_at,
        cached=cached,
        stale=cover_letter_is_stale(row, ai),
    )


@resumes_router.get("/{resume_id}/cover-letter/{vacancy_id}", response_model=CoverLetterOut)
def get_cover_letter(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> CoverLetterOut:
    """Сохранённое письмо пары; 404 — если генерация ещё не запускалась."""
    _get_own_resume(db, resume_id, user)
    row = repo.get_cover_letter(db, resume_id=resume_id, vacancy_id=vacancy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cover letter not found")
    return _letter_to_out(row, cached=True, ai=current_ai_config())


@resumes_router.post(
    "/{resume_id}/cover-letter/{vacancy_id}",
    response_model=None,
    responses={
        200: {"model": CoverLetterOut},
        202: {"model": LlmJobAccepted},
    },
)
@pat_scope("llm")
def generate_cover_letter(
    resume_id: int,
    vacancy_id: str,
    body: CoverLetterRequest | None = None,
    force: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Свежее письмо того же тона без force → 200; иначе фоновая джоба → 202 ``{job_id}``."""
    _get_own_resume(db, resume_id, user)
    vacancy = repo.get_vacancy_row(db, vacancy_id)
    if vacancy is None:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    tone = body.tone if body else "neutral"
    extra_notes = body.extra_notes if body else None
    if not force:
        existing = repo.get_cover_letter(db, resume_id=resume_id, vacancy_id=vacancy_id)
        ai = current_ai_config()
        if existing is not None and existing.tone == tone and not cover_letter_is_stale(existing, ai):
            record_cover_letter(outcome="cached")
            return _letter_to_out(existing, cached=True, ai=ai)
    try:
        result = llm_job_runner.enqueue(
            user_id=user.id,
            kind=KIND_COVER_LETTER,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            force=force,
            params={"tone": tone, "extra_notes": extra_notes},
        )
    except LlmJobUserLimitError as e:
        from backend.errors import CODE_RATE_LIMITED, http_error

        raise http_error(429, CODE_RATE_LIMITED, str(e), retryable=True) from e
    except LlmDailyQuotaError as e:
        from backend.errors import CODE_LLM_QUOTA_EXCEEDED, http_error

        raise http_error(429, CODE_LLM_QUOTA_EXCEEDED, str(e), retryable=False) from e
    accepted = LlmJobAccepted(job_id=result.job_id, status="queued")
    return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))


@resumes_router.delete("/{resume_id}/cover-letter/{vacancy_id}", status_code=204)
def delete_cover_letter(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> None:
    """Сбросить сохранённое письмо пары."""
    _get_own_resume(db, resume_id, user)
    if not repo.delete_cover_letter(db, resume_id=resume_id, vacancy_id=vacancy_id):
        raise HTTPException(status_code=404, detail="Cover letter not found")


def _tailoring_to_out(row: ResumeTailoringRow, *, cached: bool, ai: AiConfig) -> ResumeTailoringOut:
    return ResumeTailoringOut(
        resume_id=row.resume_id,
        vacancy_id=row.vacancy_id,
        summary=row.summary or "",
        suggestions=[ResumeTailoringSuggestion.model_validate(s) for s in row.suggestions or []],
        keywords=list(row.keywords or []),
        do_not_claim=list(row.do_not_claim or []),
        model=row.model,
        created_at=row.created_at,
        cached=cached,
        stale=resume_tailoring_is_stale(row, ai),
    )


@resumes_router.get("/{resume_id}/tailor/{vacancy_id}", response_model=ResumeTailoringOut)
def get_resume_tailoring(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ResumeTailoringOut:
    """Сохранённая адаптация пары; 404 — если ещё не запускалась."""
    _get_own_resume(db, resume_id, user)
    row = repo.get_resume_tailoring(db, resume_id=resume_id, vacancy_id=vacancy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Resume tailoring not found")
    return _tailoring_to_out(row, cached=True, ai=current_ai_config())


@resumes_router.post(
    "/{resume_id}/tailor/{vacancy_id}",
    response_model=None,
    responses={200: {"model": ResumeTailoringOut}, 202: {"model": LlmJobAccepted}},
)
@pat_scope("llm")
def tailor_resume(
    resume_id: int,
    vacancy_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Свежая адаптация без force → 200; иначе (нет, устарела, force) фоновая джоба → 202 ``{job_id}``."""
    _get_own_resume(db, resume_id, user)
    if repo.get_vacancy_row(db, vacancy_id) is None:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if not force:
        existing = repo.get_resume_tailoring(db, resume_id=resume_id, vacancy_id=vacancy_id)
        ai = current_ai_config()
        if existing is not None and not resume_tailoring_is_stale(existing, ai):
            return _tailoring_to_out(existing, cached=True, ai=ai)
    try:
        result = llm_job_runner.enqueue(
            user_id=user.id,
            kind=KIND_RESUME_TAILOR,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            force=force,
        )
    except LlmJobUserLimitError as e:
        from backend.errors import CODE_RATE_LIMITED, http_error

        raise http_error(429, CODE_RATE_LIMITED, str(e), retryable=True) from e
    except LlmDailyQuotaError as e:
        from backend.errors import CODE_LLM_QUOTA_EXCEEDED, http_error

        raise http_error(429, CODE_LLM_QUOTA_EXCEEDED, str(e), retryable=False) from e
    accepted = LlmJobAccepted(job_id=result.job_id, status="queued")
    return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))


@resumes_router.delete("/{resume_id}/tailor/{vacancy_id}", status_code=204)
def delete_resume_tailoring(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> None:
    """Сбросить сохранённую адаптацию пары."""
    _get_own_resume(db, resume_id, user)
    if not repo.delete_resume_tailoring(db, resume_id=resume_id, vacancy_id=vacancy_id):
        raise HTTPException(status_code=404, detail="Resume tailoring not found")


def _interview_prep_to_out(row: InterviewPrepRow, *, cached: bool, ai: AiConfig) -> InterviewPrepOut:
    return InterviewPrepOut(
        resume_id=row.resume_id,
        vacancy_id=row.vacancy_id,
        summary=row.summary or "",
        questions=[InterviewQuestion.model_validate(q) for q in row.questions or []],
        gaps=[InterviewGap.model_validate(g) for g in row.gaps or []],
        questions_to_ask=list(row.questions_to_ask or []),
        model=row.model,
        created_at=row.created_at,
        cached=cached,
        stale=interview_prep_is_stale(row, ai),
    )


@resumes_router.get("/{resume_id}/interview-prep/{vacancy_id}", response_model=InterviewPrepOut)
def get_interview_prep(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> InterviewPrepOut:
    """Сохранённая подготовка к собеседованию для пары; 404 — если ещё не запускалась."""
    _get_own_resume(db, resume_id, user)
    row = repo.get_interview_prep(db, resume_id=resume_id, vacancy_id=vacancy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Interview prep not found")
    return _interview_prep_to_out(row, cached=True, ai=current_ai_config())


@resumes_router.post(
    "/{resume_id}/interview-prep/{vacancy_id}",
    response_model=None,
    responses={200: {"model": InterviewPrepOut}, 202: {"model": LlmJobAccepted}},
)
@pat_scope("llm")
def prepare_interview(
    resume_id: int,
    vacancy_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Свежая подготовка без force → 200; иначе (нет, устарела, force) фоновая джоба → 202 ``{job_id}``."""
    _get_own_resume(db, resume_id, user)
    if repo.get_vacancy_row(db, vacancy_id) is None:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if not force:
        existing = repo.get_interview_prep(db, resume_id=resume_id, vacancy_id=vacancy_id)
        ai = current_ai_config()
        if existing is not None and not interview_prep_is_stale(existing, ai):
            return _interview_prep_to_out(existing, cached=True, ai=ai)
    try:
        result = llm_job_runner.enqueue(
            user_id=user.id,
            kind=KIND_INTERVIEW_PREP,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            force=force,
        )
    except LlmJobUserLimitError as e:
        from backend.errors import CODE_RATE_LIMITED, http_error

        raise http_error(429, CODE_RATE_LIMITED, str(e), retryable=True) from e
    except LlmDailyQuotaError as e:
        from backend.errors import CODE_LLM_QUOTA_EXCEEDED, http_error

        raise http_error(429, CODE_LLM_QUOTA_EXCEEDED, str(e), retryable=False) from e
    accepted = LlmJobAccepted(job_id=result.job_id, status="queued")
    return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))


@resumes_router.delete("/{resume_id}/interview-prep/{vacancy_id}", status_code=204)
def delete_interview_prep(
    resume_id: int,
    vacancy_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> None:
    """Сбросить сохранённую подготовку пары."""
    _get_own_resume(db, resume_id, user)
    if not repo.delete_interview_prep(db, resume_id=resume_id, vacancy_id=vacancy_id):
        raise HTTPException(status_code=404, detail="Interview prep not found")


@resumes_router.post("/{resume_id}/match-batch", status_code=202, response_model=LlmJobAccepted)
@pat_scope("llm")
def match_batch(
    resume_id: int,
    body: MatchBatchRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """LLM-оценка резюме по нескольким вакансиям (до 20) одной фоновой задачей.

    Свежие оценки не пересчитываются; квота — за каждую оценённую вакансию (в воркере).
    """
    from backend.services.match_batch_service import job_params

    _get_own_resume(db, resume_id, user)
    vacancy_ids = list(dict.fromkeys(body.vacancy_ids))
    try:
        result = llm_job_runner.enqueue(
            user_id=user.id,
            kind=KIND_MATCH_BATCH,
            resume_id=resume_id,
            params=job_params(resume_id, vacancy_ids),
        )
    except LlmJobUserLimitError as e:
        from backend.errors import CODE_RATE_LIMITED, http_error

        raise http_error(429, CODE_RATE_LIMITED, str(e), retryable=True) from e
    return LlmJobAccepted(job_id=result.job_id, status="queued")
