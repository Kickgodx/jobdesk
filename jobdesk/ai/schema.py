"""Схема JSON-ответа LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from jobdesk.config.schema import canonical_specializations, split_specializations
from jobdesk.extraction.grade_extractor import GRADE_OPTIONS, normalize_grade

_VALID_GRADES = frozenset(GRADE_OPTIONS)
_VALID_WORK_FORMATS = frozenset({"remote", "hybrid", "office", "relocation", "unknown"})

# Те же значения, что дают правила (``ingestion.rules._extract_employment``).
_EMPLOYMENT_ALIASES = {
    "full-time": "full-time",
    "full time": "full-time",
    "fulltime": "full-time",
    "полная": "full-time",
    "полная занятость": "full-time",
    "part-time": "part-time",
    "part time": "part-time",
    "parttime": "part-time",
    "частичная": "part-time",
    "частичная занятость": "part-time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "project": "contract",
    "project-based": "contract",
    "контракт": "contract",
    "гпх": "contract",
    "проектная работа": "contract",
}


def normalize_employment_type(v: object) -> str | None:
    """Занятость ответа LLM → full-time / part-time / contract; прочее — None."""
    if not isinstance(v, str):
        return None
    key = " ".join(v.strip().lower().replace("_", "-").split())
    return _EMPLOYMENT_ALIASES.get(key)


def _coerce_optional_str(v: object) -> str | None:
    """Строковые поля: модели иногда отдают list вместо str."""
    if v is None:
        return None
    if isinstance(v, list):
        parts = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return "; ".join(parts) if parts else None
    if isinstance(v, dict):
        return None
    s = str(v).strip()
    return s or None


def _coerce_str_list(v: object) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, list):
        out: list[str] = []
        for x in v:
            if x is None:
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    s = str(v).strip()
    return [s] if s else []


class VacancyAIExtraction(BaseModel):
    is_job_post: bool = True
    rejection_reason: str | None = None

    detected_specializations: list[str] = Field(
        default_factory=list,
        description=(
            "Primary hired role(s) in this post, usually one entry (e.g. ['Backend']). "
            "A role, never a language/framework/tool (Python, Go, Java go to technologies). "
            "Do not add QA, analyst, or DevOps from skill/tool mentions unless that role is explicitly hired."
        ),
    )
    detected_grade: str = "unknown"
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = Field(
        default=None,
        description=(
            "ISO currency (RUB, USD, EUR, JPY, CAD, …). "
            "salary_min/max are MONTHLY except /HOUR. "
            "/HOUR only for explicit hourly pay — keep hourly amounts. "
            "Annual (Annually, $125k TC, 26k EUR Annually): divide by 12, plain ISO (no /YEAR). "
            "Monthly («200 тр», «300к гросс», /month) → plain ISO, do not divide."
        ),
    )
    work_format: str | None = None
    country: str | None = None
    city: str | None = None
    company: str | None = None
    contact: str | None = None
    technologies: list[str] = Field(
        default_factory=list,
        description=(
            "Languages, frameworks, databases, infra and tools required by the post "
            "(canonical names: Go, PostgreSQL). Not roles, not soft skills."
        ),
    )
    required_experience: str | None = None
    employment_type: str | None = Field(
        default=None,
        description="One of full-time, part-time, contract; null if the post does not state it.",
    )
    application_urls: list[str] = Field(default_factory=list)

    confidence: float = Field(default=0.5, ge=0, le=1)
    notes: str | None = Field(
        default=None,
        description="Краткие пояснения на русском: неоднозначности, пропуски в тексте, допущения; null если нечего добавить.",
    )

    @field_validator(
        "rejection_reason",
        "country",
        "city",
        "company",
        "contact",
        "required_experience",
        "notes",
        mode="before",
    )
    @classmethod
    def norm_optional_str_fields(cls, v: object) -> str | None:
        return _coerce_optional_str(v)

    @field_validator("employment_type", mode="before")
    @classmethod
    def norm_employment_type(cls, v: object) -> str | None:
        return normalize_employment_type(_coerce_optional_str(v))

    @field_validator(
        "detected_specializations",
        "technologies",
        "application_urls",
        mode="before",
    )
    @classmethod
    def norm_str_list_fields(cls, v: object) -> list[str]:
        return _coerce_str_list(v)

    @field_validator("detected_specializations")
    @classmethod
    def norm_specializations(cls, v: list[str]) -> list[str]:
        # AQA / Manual QA от модели и в кэше extraction до extract-v9.
        return canonical_specializations(v)

    @model_validator(mode="after")
    def split_roles_and_technologies(self) -> VacancyAIExtraction:
        # Язык в специализациях (ответ модели или кэш до extract-v10) — это технология.
        self.detected_specializations, self.technologies = split_specializations(
            self.detected_specializations, self.technologies
        )
        return self

    @field_validator("detected_grade", mode="before")
    @classmethod
    def norm_grade(cls, v: object) -> str:
        return normalize_grade(v)

    @field_validator("work_format", mode="before")
    @classmethod
    def norm_work_format(cls, v: object) -> str | None:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        wf = str(v).strip().lower()
        if wf in _VALID_WORK_FORMATS:
            return wf if wf != "unknown" else None
        return None

    @field_validator("salary_currency", mode="before")
    @classmethod
    def norm_currency(cls, v: object) -> str | None:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        from jobdesk.fx.convert import parse_salary_currency

        # Сохраняем период (…/HOUR): normalize_currency оставляет только ISO.
        iso, period = parse_salary_currency(str(v).strip())
        if not iso:
            return None
        if period == "hour":
            return f"{iso}/HOUR"
        if period == "year":
            return f"{iso}/YEAR"
        return iso


Importance = Literal["critical", "important", "secondary"]

_IMPORTANCE_ALIASES = {
    "critical": "critical",
    "required": "critical",
    "must": "critical",
    "must_have": "critical",
    "mandatory": "critical",
    "important": "important",
    "nice": "secondary",
    "nice_to_have": "secondary",
    "optional": "secondary",
    "secondary": "secondary",
}


def _clamp_score(v: object) -> int:
    """Модели иногда отдают float/str — приводим к int 0..100."""
    try:
        n = round(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _coerce_skill_match(v: object) -> object:
    """Терпим формат ``["python"]`` — строка превращается в SkillMatch."""
    if isinstance(v, str):
        return {"skill": v.strip(), "importance": "important", "evidence": ""}
    return v


class SkillMatch(BaseModel):
    """Один навык в matching/missing: название, приоритет требования, обоснование."""

    skill: str
    importance: Importance = "important"
    evidence: str = ""

    @field_validator("importance", mode="before")
    @classmethod
    def norm_importance(cls, v: object) -> str:
        key = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return _IMPORTANCE_ALIASES.get(key, "important")

    @field_validator("skill", "evidence", mode="before")
    @classmethod
    def norm_str(cls, v: object) -> str:
        return str(v).strip() if v is not None else ""


class MatchBreakdown(BaseModel):
    """Оценки LLM по измерениям; итоговый score считает backend."""

    role_match: int = Field(default=0, ge=0, le=100)
    skills_match: int = Field(default=0, ge=0, le=100)
    seniority_match: int = Field(default=0, ge=0, le=100)
    domain_match: int = Field(default=0, ge=0, le=100)
    work_format_match: int = Field(default=0, ge=0, le=100)

    @field_validator(
        "role_match",
        "skills_match",
        "seniority_match",
        "domain_match",
        "work_format_match",
        mode="before",
    )
    @classmethod
    def norm_score(cls, v: object) -> int:
        return _clamp_score(v)


class VacancyMatchExtraction(BaseModel):
    """Оценка соответствия резюме вакансии (ответ LLM матчинга).

    Модель оценивает только измерения и объясняет; ``overall_score``
    вычисляется детерминированно через :func:`calculate_overall_score`.
    """

    confidence: int = Field(
        default=50,
        ge=0,
        le=100,
        description="0-100: уверенность в качестве оценки (полнота резюме/вакансии), не сам score.",
    )
    breakdown: MatchBreakdown = Field(default_factory=MatchBreakdown)
    matching_skills: list[SkillMatch] = Field(default_factory=list, max_length=8)
    missing_skills: list[SkillMatch] = Field(default_factory=list, max_length=8)
    strengths: list[str] = Field(default_factory=list, max_length=5)
    risks: list[str] = Field(default_factory=list, max_length=5)
    explanation: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_confidence(cls, v: object) -> int:
        return _clamp_score(v)

    @field_validator("explanation", mode="before")
    @classmethod
    def norm_explanation(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("matching_skills", "missing_skills", mode="before")
    @classmethod
    def norm_skill_list_fields(cls, v: object) -> list:
        if not isinstance(v, list):
            return []
        return [_coerce_skill_match(x) for x in v][:8]

    @field_validator("strengths", "risks", mode="before")
    @classmethod
    def norm_str_list_fields(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:5]


def calculate_overall_score(
    breakdown: MatchBreakdown,
    missing_skills: list[SkillMatch],
) -> int:
    """Детерминированный итоговый score: взвешенная сумма + penalty за critical-пробелы."""
    score = round(
        breakdown.role_match * 0.30
        + breakdown.skills_match * 0.35
        + breakdown.seniority_match * 0.20
        + breakdown.domain_match * 0.10
        + breakdown.work_format_match * 0.05
    )

    critical_missing = sum(1 for s in missing_skills if s.importance == "critical")
    if critical_missing >= 2:
        score = min(score, 59)
    elif critical_missing == 1:
        score = min(score, 69)

    return max(0, min(100, score))


TailoringPriority = Literal["high", "medium", "low"]


class ResumeTailoringSuggestion(BaseModel):
    """Одна правка резюме под вакансию: что было, как переписать, какое требование закрывает."""

    section: str = ""
    current: str = ""
    suggested: str = ""
    reason: str = ""
    priority: TailoringPriority = "medium"

    @field_validator("section", "current", "suggested", "reason", mode="before")
    @classmethod
    def norm_str(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("priority", mode="before")
    @classmethod
    def norm_priority(cls, v: object) -> str:
        key = str(v or "").strip().lower()
        return key if key in ("high", "medium", "low") else "medium"


class ResumeTailoringExtraction(BaseModel):
    """Ответ LLM адаптации резюме: только переформулировки фактов из резюме, без выдумок."""

    summary: str = ""
    suggestions: list[ResumeTailoringSuggestion] = Field(default_factory=list, max_length=8)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    do_not_claim: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("summary", mode="before")
    @classmethod
    def norm_summary(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("suggestions", mode="before")
    @classmethod
    def norm_suggestions(cls, v: object) -> list:
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict)][:8]

    @field_validator("keywords", mode="before")
    @classmethod
    def norm_keywords(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:10]

    @field_validator("do_not_claim", mode="before")
    @classmethod
    def norm_do_not_claim(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:6]


InterviewQuestionKind = Literal["technical", "experience", "behavioral", "gap"]
_QUESTION_KINDS = ("technical", "experience", "behavioral", "gap")


class InterviewQuestion(BaseModel):
    """Вероятный вопрос собеседования и тезисы ответа из реального опыта резюме."""

    kind: InterviewQuestionKind = "technical"
    topic: str = ""
    question: str = ""
    why: str = ""
    answer_points: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("topic", "question", "why", mode="before")
    @classmethod
    def norm_str(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("kind", mode="before")
    @classmethod
    def norm_kind(cls, v: object) -> str:
        key = str(v or "").strip().lower()
        return key if key in _QUESTION_KINDS else "technical"

    @field_validator("answer_points", mode="before")
    @classmethod
    def norm_points(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:4]


class InterviewGap(BaseModel):
    """Требование вакансии, которого нет в резюме, и как к вопросу о нём подготовиться."""

    skill: str = ""
    advice: str = ""

    @field_validator("skill", "advice", mode="before")
    @classmethod
    def norm_str(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""


class InterviewPrepExtraction(BaseModel):
    """Ответ LLM подготовки к собеседованию: вопросы, тезисы из резюме, пробелы, вопросы работодателю."""

    summary: str = ""
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=10)
    gaps: list[InterviewGap] = Field(default_factory=list, max_length=5)
    questions_to_ask: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("summary", mode="before")
    @classmethod
    def norm_summary(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("questions", mode="before")
    @classmethod
    def drop_bad_questions(cls, v: object) -> list:
        items = v if isinstance(v, list) else []
        return [q for q in items if isinstance(q, dict) and _coerce_optional_str(q.get("question"))][:10]

    @field_validator("gaps", mode="before")
    @classmethod
    def drop_bad_gaps(cls, v: object) -> list:
        items = v if isinstance(v, list) else []
        return [g for g in items if isinstance(g, dict) and _coerce_optional_str(g.get("skill"))][:5]

    @field_validator("questions_to_ask", mode="before")
    @classmethod
    def norm_ask(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:5]


# «Спросить базу»: план запроса к каталогу. Модель выбирает только фильтры и разделы цифр,
# сами цифры считает backend — модель не пишет SQL и не видит строк вакансий.
MarketTopic = Literal["count", "salary", "companies", "technologies", "trend", "lifetime", "channels", "examples"]
MARKET_TOPICS: tuple[str, ...] = (
    "count",
    "salary",
    "companies",
    "technologies",
    "trend",
    "lifetime",
    "channels",
    "examples",
)


class MarketQueryFilters(BaseModel):
    """Фильтры каталога из вопроса; пустое поле — без фильтра."""

    specializations: list[str] = Field(default_factory=list, max_length=5)
    technologies: list[str] = Field(default_factory=list, max_length=5)
    grade: str | None = None
    work_format: str | None = None
    date_from: str | None = Field(default=None, description="YYYY-MM-DD")
    date_to: str | None = Field(default=None, description="YYYY-MM-DD")
    has_salary: bool | None = None
    text_query: str | None = Field(default=None, description="Слова для полнотекстового поиска, если нет фильтра.")

    @field_validator("specializations", "technologies", mode="before")
    @classmethod
    def norm_lists(cls, v: object) -> list[str]:
        return _coerce_str_list(v)[:5]

    @field_validator("grade", "work_format", "date_from", "date_to", "text_query", mode="before")
    @classmethod
    def norm_optional(cls, v: object) -> str | None:
        return _coerce_optional_str(v)


class MarketQueryVariant(BaseModel):
    """Вариант сравнения: свои фильтры поверх общих («Go» против «Python»)."""

    label: str = ""
    filters: MarketQueryFilters = Field(default_factory=MarketQueryFilters)

    @field_validator("label", mode="before")
    @classmethod
    def norm_label(cls, v: object) -> str:
        return (_coerce_optional_str(v) or "")[:60]

    @field_validator("filters", mode="before")
    @classmethod
    def norm_filters(cls, v: object) -> object:
        return v if isinstance(v, (dict, MarketQueryFilters)) else {}


class MarketQueryPlan(BaseModel):
    """Ответ LLM на первом шаге «Спросить базу»: можно ли ответить по каталогу и что посчитать."""

    answerable: bool = True
    reason: str = Field(default="", description="Почему по базе не ответить, или пусто.")
    filters: MarketQueryFilters = Field(default_factory=MarketQueryFilters)
    topics: list[MarketTopic] = Field(default_factory=list)
    variants: list[MarketQueryVariant] = Field(default_factory=list, max_length=4)

    @field_validator("reason", mode="before")
    @classmethod
    def norm_reason(cls, v: object) -> str:
        return _coerce_optional_str(v) or ""

    @field_validator("filters", mode="before")
    @classmethod
    def norm_filters(cls, v: object) -> object:
        # При answerable=false модель часто отдаёт filters: null.
        return v if isinstance(v, (dict, MarketQueryFilters)) else {}

    @field_validator("topics", mode="before")
    @classmethod
    def norm_topics(cls, v: object) -> list[str]:
        out: list[str] = []
        for t in _coerce_str_list(v):
            key = t.strip().lower()
            if key in MARKET_TOPICS and key not in out:
                out.append(key)
        return out

    @field_validator("variants", mode="before")
    @classmethod
    def norm_variants(cls, v: object) -> list:
        items = v if isinstance(v, list) else []
        return [x for x in items if isinstance(x, dict)][:4]
