"""Резюме против рынка: что платят и что требуют в вакансиях, ближайших к резюме по смыслу.

Ближайшие — уникальные вакансии (с архивом: это история рынка) за ``WINDOW_DAYS`` по
косинусному расстоянию эмбеддингов резюме и вакансии. Из них — профиль (грейды, роли),
частые технологии с отметкой «есть в резюме» и квантили рублёвой зарплаты. LLM не нужен:
вектор резюме уже хранится для «Подбора под резюме».
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.db.models import ResumeRow, VacancyEmbeddingRow, VacancyRow
from backend.db.vacancy_metrics import _prepare_fx
from backend.db.vector_type import query_vector
from backend.schemas import MarketShare, ResumeMarketOut, ResumeMarketTechnology
from backend.services import embedding_service
from backend.services.skill_gap_service import technology_matcher
from jobdesk.analytics.skill_gaps import mentions

WINDOW_DAYS = 365
# Профиль и технологии — по стольким ближайшим вакансиям (выбирает клиент в этих пределах).
DEFAULT_PEERS = 100
MIN_PEERS = 10
# HNSW отдаёт не больше ef_search строк, а pgvector не даёт поднять его выше 1000.
MAX_PEERS = 500
# Квантили зарплаты — по всем вакансиям с зарплатой среди ближайших, но не меньше стольких
# ближайших с зарплатой из CANDIDATES (у большинства вакансий зарплаты нет).
SALARY_PEERS = 30
MIN_SALARY_PEERS = 8
CANDIDATES = 200
TOP_PROFILE = 3
TOP_TECHNOLOGIES = 12
# Технология реже этой доли ближайших — не «частая», а шум одной-двух вакансий.
MIN_TECH_SHARE = 0.1


def quantile(sorted_values: list[float], p: float) -> float:
    """Квантиль с линейной интерполяцией — как ``percentile_cont`` в Postgres."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = p * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _shares(counter: Counter, total: int, limit: int) -> list[MarketShare]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [MarketShare(name=name, share=n / total) for name, n in items]


def nearest_vacancies(
    session: Session,
    vector: list[float],
    *,
    model: str,
    now: datetime,
    limit: int = CANDIDATES,
    specialization: str | None = None,
    grade: str | None = None,
) -> list:
    """Ближайшие к вектору уникальные вакансии за окно: (грейд, роли, технологии, зарплата ₽).

    ``specialization`` — только вакансии этой роли, ``grade`` — только этого грейда.
    """
    e = VacancyEmbeddingRow
    v = VacancyRow
    distance = e.embedding.cosine_distance(query_vector(vector))
    # HNSW отдаёт не больше ef_search строк (по умолчанию 40) — поднимаем до числа кандидатов.
    session.execute(text(f"SET LOCAL hnsw.ef_search = {int(limit)}"))
    conditions = [
        e.model == model,
        v.is_duplicate.is_(False),
        v.post_date >= now - timedelta(days=WINDOW_DAYS),
    ]
    if specialization or grade:
        # Фильтр по роли или грейду отсекает большую часть ближайших: без iterative scan
        # (pgvector ≥ 0.8) HNSW отдал бы ef_search ближайших вообще, и после фильтра осталось бы
        # несколько строк.
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    if specialization:
        conditions.append(v.detected_specializations.contains([specialization]))
    if grade:
        conditions.append(v.detected_grade == grade)
    stmt = (
        select(v.detected_grade, v.detected_specializations, v.technologies, v.detected_salary_avg_rub)
        .select_from(e)
        .join(v, v.id == e.vacancy_id)
        .where(*conditions)
        .order_by(distance, e.vacancy_id)
        .limit(limit)
    )
    return session.execute(stmt).all()


def resume_technology_check(session: Session, resume: ResumeRow) -> tuple[list[str], Callable[[str], bool]]:
    """Технологии резюме по словарю и проверка «технология есть в резюме».

    Словарь (с алиасами: Golang → Go) или само название в тексте («Atlassian Jira» → Jira).
    """
    resume_techs = technology_matcher(session).find(resume.text)
    own = {t.lower() for t in resume_techs}
    found: dict[str, bool] = {}

    def in_resume(tech: str) -> bool:
        key = tech.lower()
        if key not in found:
            found[key] = key in own or mentions(resume.text, tech)
        return found[key]

    return resume_techs, in_resume


def resume_market(
    session: Session, resume: ResumeRow, *, peers_limit: int = DEFAULT_PEERS, grade: str | None = None
) -> ResumeMarketOut:
    """Профиль рынка для резюме по ``peers_limit`` ближайшим (``grade`` — только этого грейда).

    ``EmbeddingsUnavailable`` — эмбеддинги выключены или недоступны.
    """
    vector = embedding_service.resume_embedding(session, resume)
    model = embedding_service._ai().embeddings.model
    rows = nearest_vacancies(
        session, vector, model=model, now=datetime.now(UTC), limit=max(CANDIDATES, peers_limit), grade=grade
    )
    salary_display = _prepare_fx(session)
    resume_techs, in_resume = resume_technology_check(session, resume)

    peers = rows[:peers_limit]
    out = ResumeMarketOut(
        resume_id=resume.id,
        window_days=WINDOW_DAYS,
        grade=grade,
        peers=len(peers),
        resume_technologies=resume_techs,
        salary_display=salary_display,
    )
    if not peers:
        return out

    grades = Counter(g for g, *_ in peers if g and g != "unknown")
    roles = Counter(r for _, specs, *_ in peers for r in set(specs or []))
    techs = Counter(t for _, _, ts, _ in peers for t in set(ts or []))
    coverages = [sum(1 for t in set(ts) if in_resume(t)) / len(set(ts)) for _, _, ts, _ in peers if ts]
    top_techs = [
        ResumeMarketTechnology(name=s.name, share=s.share, in_resume=in_resume(s.name))
        for s in _shares(techs, len(peers), TOP_TECHNOLOGIES)
        if s.share >= MIN_TECH_SHARE
    ]
    # Зарплаты — той же выборки; у большинства вакансий зарплаты нет, поэтому при малой выборке
    # добираем ближайших с зарплатой из всех кандидатов.
    salaries = [float(s) for *_, s in peers if s is not None]
    if len(salaries) < SALARY_PEERS:
        salaries = [float(s) for *_, s in rows if s is not None][:SALARY_PEERS]
    salaries.sort()
    update: dict = {
        "grades": _shares(grades, len(peers), TOP_PROFILE),
        "specializations": _shares(roles, len(peers), TOP_PROFILE),
        "technologies": top_techs,
        "coverage": sum(coverages) / len(coverages) if coverages else None,
    }
    if len(salaries) >= MIN_SALARY_PEERS:
        update.update(
            salary_count=len(salaries),
            p10=quantile(salaries, 0.1),
            p25=quantile(salaries, 0.25),
            median=quantile(salaries, 0.5),
            p75=quantile(salaries, 0.75),
            p90=quantile(salaries, 0.9),
        )
    return out.model_copy(update=update)
