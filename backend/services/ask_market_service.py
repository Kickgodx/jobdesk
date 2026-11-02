"""«Спросить базу»: вопрос о рынке → план (фильтры каталога + разделы цифр) → факты → ответ.

Два вызова модели в одной LLM-задаче (``kind=ask_market``): план — strict JSON по белому
списку полей, без SQL; цифры считает backend теми же функциями, что аналитика; ответ модель
пишет только по ним. В квоте пользователя вопрос — один вызов. История диалога (2–3 прошлых
вопроса с их фильтрами) приходит от UI и хранится только во вкладке браузера.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Float, cast, func, literal_column, select
from sqlalchemy.orm import Session

from backend.db import analytics, technology_analytics, vacancy_lifetime
from backend.db import llm_job_repository as llm_jobs
from backend.db.models import VacancyRow
from backend.db.session import session_scope
from backend.db.vacancy_filters import apply_vacancy_filters
from backend.db.vacancy_metrics import technology_counts
from backend.schemas import VacancyQuery
from backend.services.interactive_llm import get_interactive_client
from backend.services.llm_job_service import LlmJobCancelled
from backend.settings import get_settings
from jobdesk.ai.prompts import ASK_MARKET_PROMPT_VERSION
from jobdesk.ai.schema import MarketQueryFilters, MarketQueryPlan
from jobdesk.config.loader import load_config
from jobdesk.config.schema import AppConfig, canonical_technologies, split_specializations
from jobdesk.extraction.grade_extractor import GRADE_OPTIONS, extract_grade, normalize_grade

_LOG = logging.getLogger(__name__)

QUESTION_MAX_CHARS = 500
HISTORY_MAX_TURNS = 3
HISTORY_ANSWER_CHARS = 600
WORK_FORMATS = ("remote", "office", "hybrid", "relocation")
TOP_N = 10
EXAMPLES = 5
TREND_MONTHS = 12
# Группа (грейд, формат) в фактах — от стольких вакансий с зарплатой.
MIN_SALARY_GROUP = 3


# --- План → фильтры каталога -------------------------------------------------------------


def _match_role(name: str, roles: dict[str, list[str]]) -> str | None:
    """Роль словаря: точное имя или синоним, иначе самый длинный синоним целыми словами
    («Backend developer» → Backend, «senior QA automation» → Automation QA)."""
    key = " ".join(name.strip().lower().split())
    best: tuple[int, str] | None = None
    for role, synonyms in roles.items():
        names = [role.lower(), *(s.lower() for s in synonyms)]
        if key in names:
            return role
        for syn in names:
            longer = best is None or len(syn) > best[0]
            if len(syn) >= 2 and longer and re.search(rf"(?<![\w]){re.escape(syn)}(?![\w])", key):
                best = (len(syn), role)
    return best[1] if best else None


def _match_format(name: str | None, cfg: AppConfig) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in WORK_FORMATS:
        return key
    for fmt, synonyms in cfg.merged_work_format_synonyms().items():
        if fmt in WORK_FORMATS and key in (s.lower() for s in synonyms):
            return fmt
    return None


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def normalize_filters(f: MarketQueryFilters, cfg: AppConfig) -> tuple[dict[str, Any], list[str]]:
    """Фильтры плана → поля ``VacancyQuery`` с каноническими именами; второе — что не распознано."""
    roles = cfg.merged_specializations()
    specs, techs = split_specializations(list(f.specializations), list(f.technologies))
    matched: list[str] = []
    dropped: list[str] = []
    for s in specs:
        role = _match_role(s, roles)
        if role is None:
            dropped.append(s)
        elif role not in matched:
            matched.append(role)
    grade = normalize_grade(f.grade) if f.grade else None
    if grade not in GRADE_OPTIONS or grade == "unknown":
        if f.grade:
            dropped.append(f.grade)
        grade = None
    work_format = _match_format(f.work_format, cfg)
    if f.work_format and work_format is None:
        dropped.append(f.work_format)
    date_from, date_to = _parse_date(f.date_from), _parse_date(f.date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    q = (f.text_query or "").strip()[:100] or None
    out = {
        "specializations": matched,
        "technologies": canonical_technologies(techs)[:5],
        "grade": grade,
        "work_format": work_format,
        "date_from": date_from,
        "date_to": date_to,
        # Модель в strict-режиме ставит false вместо null — «только без зарплаты» никто не спрашивает.
        "has_salary": True if f.has_salary else None,
        "q": q,
    }
    return out, dropped


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Вариант сравнения поверх общих фильтров: непустые поля варианта заменяют общие."""
    merged = dict(base)
    for key, value in override.items():
        if value not in (None, [], ""):
            merged[key] = value
    return merged


def to_query(filters: dict[str, Any]) -> VacancyQuery:
    # История рынка — с архивом, как во вкладке «Аналитика»; активные считаются отдельно.
    return VacancyQuery(include_archived=True, **filters)


def _filters_json(filters: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in filters.items() if v not in (None, [], "")}


# --- Факты -----------------------------------------------------------------------------


def _r(v: float | None) -> int | None:
    return round(v) if v is not None else None


def _count(session: Session, query: VacancyQuery) -> dict[str, Any]:
    v = VacancyRow
    sal = v.detected_salary_avg_rub
    q = lambda p: cast(func.percentile_cont(p).within_group(sal), Float)  # noqa: E731
    total, active, with_salary, first, last, p25, p50, p75 = session.execute(
        apply_vacancy_filters(
            select(
                func.count(),
                func.count().filter(v.archived_at.is_(None)),
                func.count(sal),
                func.min(v.post_date),
                func.max(v.post_date),
                q(0.25),
                q(0.5),
                q(0.75),
            ),
            query,
        )
    ).one()
    return {
        "vacancies": int(total or 0),
        "active": int(active or 0),
        "with_salary": int(with_salary or 0),
        "first_post": first.date().isoformat() if first else None,
        "last_post": last.date().isoformat() if last else None,
        "salary": {"p25": _r(p25), "median": _r(p50), "p75": _r(p75)} if with_salary else None,
    }


def _groups(rows) -> list[dict[str, Any]]:
    return [
        {"group": g.group, "n": g.count, "p25": _r(g.q1), "median": _r(g.median), "p75": _r(g.q3)}
        for g in rows
        if g.count >= MIN_SALARY_GROUP
    ]


def _salary(session: Session, query: VacancyQuery) -> dict[str, Any]:
    s = analytics.salary_analytics(session, query)
    return {"by_grade": _groups(s.by_grade), "by_work_format": _groups(s.by_work_format)}


def _trend(session: Session, query: VacancyQuery) -> list[dict[str, Any]]:
    v = VacancyRow
    month = func.date_trunc(literal_column("'month'"), v.post_date)
    rows = session.execute(
        apply_vacancy_filters(
            select(
                month,
                func.count(),
                func.count(v.detected_salary_avg_rub),
                cast(func.percentile_cont(0.5).within_group(v.detected_salary_avg_rub), Float),
            )
            .where(v.post_date.isnot(None))
            .group_by(month)
            .order_by(month.desc())
            .limit(TREND_MONTHS),
            query,
        )
    ).all()
    current = datetime.now(UTC).strftime("%Y-%m")
    out = []
    for m, n, ns, med in reversed(rows):
        month = m.strftime("%Y-%m")
        item = {"month": month, "vacancies": int(n), "with_salary": int(ns), "median": _r(med) if ns else None}
        if month == current:
            # Месяц не закончился: число вакансий в нём нельзя сравнивать с полными месяцами.
            item["partial"] = True
        out.append(item)
    return out


def _companies(session: Session, query: VacancyQuery) -> list[dict[str, Any]]:
    return [
        {"company": c.company, "vacancies": c.vacancies, "avg_salary": _r(c.avg_salary_rub)}
        for c in analytics.top_companies(session, query, limit=TOP_N)
    ]


def _technologies(session: Session, query: VacancyQuery, total: int) -> dict[str, Any]:
    counts = technology_counts(session, query, limit=15)
    t = technology_analytics.technology_analytics(session, query)
    return {
        "most_required": [{"name": k, "vacancies": n, "share": round(n / total, 3)} for k, n in counts.items()]
        if total
        else [],
        "salary_premium": [
            {"name": p.technology, "premium": round(p.premium, 3), "vacancies": p.compared_vacancies}
            for p in sorted(t.premium, key=lambda p: -(p.premium or 0))
            if p.premium is not None
        ][:TOP_N],
        "rising": [{"name": m.technology, "delta_pp": round(m.delta_pp, 1)} for m in t.momentum if m.delta_pp > 0][:5],
        "falling": [
            {"name": m.technology, "delta_pp": round(m.delta_pp, 1)} for m in reversed(t.momentum) if m.delta_pp < 0
        ][:5],
        "momentum_window_days": t.momentum_window_days,
    }


def _lifetime(session: Session, query: VacancyQuery) -> dict[str, Any]:
    life = vacancy_lifetime.vacancy_lifetime(session, query)
    return {
        "telegram_vacancies": life.vacancies,
        "republished_share": round(life.republished_share, 3),
        "long_open": life.long_open,
        "long_open_days": life.long_open_days,
        "span_median_days_of_republished": life.span_p50_days,
        "companies_republishing": [
            {"company": c.company, "republished": c.republished, "max_span_days": c.max_span_days}
            for c in life.companies[:5]
        ],
    }


def _channels(session: Session, query: VacancyQuery) -> list[dict[str, Any]]:
    items = sorted(analytics.source_quality(session, query), key=lambda s: -s.vacancies)[:TOP_N]
    return [{"channel": s.channel, "vacancies": s.vacancies, "unique_share": round(s.unique_share, 3)} for s in items]


def _examples(session: Session, query: VacancyQuery) -> list[dict[str, Any]]:
    v = VacancyRow
    rows = session.execute(
        apply_vacancy_filters(
            select(
                v.id,
                v.post_date,
                v.company,
                v.detected_specializations,
                v.detected_grade,
                v.detected_salary_avg_rub,
                v.work_format,
            )
            .order_by(v.post_date.desc().nulls_last(), v.id)
            .limit(EXAMPLES),
            query.model_copy(update={"include_archived": False}),
        )
    ).all()
    return [
        {
            "id": r.id,
            "date": r.post_date.date().isoformat() if r.post_date else None,
            "company": r.company,
            "roles": list(r.detected_specializations or []),
            "grade": r.detected_grade,
            "salary": _r(r.detected_salary_avg_rub),
            "work_format": r.work_format,
        }
        for r in rows
    ]


def compute_facts(
    session: Session, filters: dict[str, Any], topics: list[str], variants: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    query = to_query(filters)
    count = _count(session, query)
    facts: dict[str, Any] = {"today": datetime.now(UTC).date().isoformat(), "filters": _filters_json(filters)}
    facts["count"] = count
    if count["vacancies"]:
        if "salary" in topics:
            facts["salary"] = _salary(session, query)
        if "trend" in topics:
            facts["trend"] = _trend(session, query)
        if "companies" in topics:
            facts["companies"] = _companies(session, query)
        if "technologies" in topics:
            facts["technologies"] = _technologies(session, query, count["vacancies"])
        if "lifetime" in topics:
            facts["lifetime"] = _lifetime(session, query)
        if "channels" in topics:
            facts["channels"] = _channels(session, query)
        if "examples" in topics:
            facts["examples"] = _examples(session, query)
    if variants:
        out = []
        for label, vf in variants:
            vq = to_query(vf)
            item: dict[str, Any] = {"label": label, "filters": _filters_json(vf), "count": _count(session, vq)}
            if item["count"]["vacancies"] and "technologies" in topics:
                tc = technology_counts(session, vq, limit=5)
                item["top_technologies"] = list(tc)
            if item["count"]["vacancies"] and "salary" in topics:
                item["salary_by_grade"] = _salary(session, vq)["by_grade"]
            out.append(item)
        facts["variants"] = out
    return facts


# --- Джоба -----------------------------------------------------------------------------


def _history_json(history: list[dict]) -> str:
    turns = [
        {
            "question": str(h.get("question") or "")[:QUESTION_MAX_CHARS],
            "filters": h.get("filters") or {},
            "answer": str(h.get("answer") or "")[:HISTORY_ANSWER_CHARS],
        }
        for h in history[-HISTORY_MAX_TURNS:]
        if isinstance(h, dict)
    ]
    return json.dumps(turns, ensure_ascii=False) if turns else "[]"


def answer_question(
    *, question: str, history: list[dict], is_cancelled: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """План → факты → ответ. Сессия БД держится только на расчёт фактов, не на вызовы модели."""
    cfg = load_config(get_settings().config_path)
    client = get_interactive_client()
    plan, usage = client.market_plan(
        question=question[:QUESTION_MAX_CHARS],
        history_json=_history_json(history),
        today=datetime.now(UTC).date().isoformat(),
        roles=", ".join(cfg.merged_specializations()),
        grades=", ".join(g for g in GRADE_OPTIONS if g != "unknown"),
        formats=", ".join(WORK_FORMATS),
    )
    if is_cancelled is not None and is_cancelled():
        raise LlmJobCancelled("Вопрос отменён — ответ не сохранён", model_answered=True)
    return _answer_from_plan(question=question, plan=plan, plan_model=usage.model, cfg=cfg, is_cancelled=is_cancelled)


def _answer_from_plan(
    *,
    question: str,
    plan: MarketQueryPlan,
    plan_model: str,
    cfg: AppConfig,
    is_cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    filters, dropped = normalize_filters(plan.filters, cfg)
    if filters["grade"] is None and not plan.variants:
        # Страховка: грейд, названный в вопросе («senior», «мидл»), модель иногда теряет.
        grade = extract_grade(question)
        if grade != "unknown":
            filters["grade"] = grade
    variants: list[tuple[str, dict[str, Any]]] = []
    for i, var in enumerate(plan.variants):
        vf, vdropped = normalize_filters(var.filters, cfg)
        dropped += vdropped
        variants.append((var.label or f"Вариант {i + 1}", _merge(filters, vf)))
    result: dict[str, Any] = {
        "question": question,
        "answerable": plan.answerable,
        "filters": _filters_json(filters),
        "topics": list(plan.topics),
        "variants": [{"label": label, "filters": _filters_json(vf)} for label, vf in variants],
        "unrecognized": list(dict.fromkeys(dropped)),
        "prompt_version": ASK_MARKET_PROMPT_VERSION,
        "model": plan_model,
    }
    if not plan.answerable:
        result.update(
            answer=plan.reason or "По базе вакансий на этот вопрос не ответить.",
            facts=None,
            filters={},
            topics=[],
            variants=[],
            unrecognized=[],
        )
        return result
    with session_scope() as session:
        facts = compute_facts(session, filters, list(plan.topics), variants)
    if dropped:
        facts["unrecognized_filters"] = result["unrecognized"]
    answer, usage = get_interactive_client().market_answer(
        question=question[:QUESTION_MAX_CHARS], facts_json=json.dumps(facts, ensure_ascii=False, default=str)
    )
    if is_cancelled is not None and is_cancelled():
        raise LlmJobCancelled("Вопрос отменён — ответ не сохранён", model_answered=True)
    result.update(answer=answer, facts=facts, model=usage.model)
    return result


def run_ask_market_job(job_id: uuid.UUID, *, params: dict, lease_owner: str, is_cancelled: Callable[[], bool]) -> bool:
    """Ответ пишется в ``llm_jobs.result``; False — lease уже не наш, результат отброшен."""
    result = answer_question(
        question=str(params.get("question") or ""),
        history=list(params.get("history") or []),
        is_cancelled=is_cancelled,
    )
    _LOG.info(
        "Спросить базу: job %s, фильтры %s, разделы %s, модель %s",
        job_id,
        result.get("filters"),
        result.get("topics"),
        result.get("model"),
    )
    with session_scope() as session:
        return llm_jobs.patch_llm_job_result(session, job_id, result, lease_owner=lease_owner)
