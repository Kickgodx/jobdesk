"""Pydantic-схемы REST API."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator, model_validator

from backend.services.api_tokens import DEFAULT_TTL_DAYS, MAX_TTL_DAYS
from jobdesk.ai.schema import (
    InterviewGap,
    InterviewQuestion,
    MatchBreakdown,
    ResumeTailoringSuggestion,
    SkillMatch,
)
from jobdesk.config.schema import split_specializations
from jobdesk.models.metrics import MetricsSummary, SalaryDisplayMeta
from jobdesk.models.vacancy import Vacancy

# partial — прогон дошёл до конца, но часть постов не разобрана (LLM отключён / много ошибок в primary).
RunStatus = Literal["queued", "running", "completed", "partial", "cancelled", "failed"]


class _RunOverrides(BaseModel):
    """Whitelist overrides прогона: неизвестное поле → 422, а не молчаливый merge в конфиг.

    Всё, чего здесь нет (``ai.base_url``, лимиты и concurrency, путь к Telethon-сессии),
    берётся только из серверного ``config.yaml``.
    """

    model_config = ConfigDict(extra="forbid")


class RunSearchOverrides(_RunOverrides):
    date_from: str | None = Field(default=None, max_length=32)
    date_to: str | None = Field(default=None, max_length=32)


class RunFiltersOverrides(_RunOverrides):
    specializations: list[str] | None = None
    any_specialization: bool | None = None
    technologies: list[str] | None = None
    exclude_keywords: list[str] | None = None
    countries: list[str] | None = None
    cities: list[str] | None = None
    work_formats: list[str] | None = None


class RunParserOverrides(_RunOverrides):
    vacancy_score_threshold: int | None = Field(default=None, ge=0, le=100)
    vacancy_min_substantive_score: int | None = Field(default=None, ge=0, le=100)
    # false — разово перечитать окно целиком, не глядя на чекпоинты каналов.
    incremental_fetch: bool | None = None


class RunAiOverrides(_RunOverrides):
    enabled: bool | None = None
    # id из llm_models.yaml — проверяется в build_config.
    model: str | None = Field(default=None, min_length=1)
    fallback_models: list[str] | None = None
    use_cache: bool | None = None


class RunCreateRequest(_RunOverrides):
    channels: list[str] | None = None
    search: RunSearchOverrides | None = None
    filters: RunFiltersOverrides | None = None
    parser: RunParserOverrides | None = None
    ai: RunAiOverrides | None = None


class RunCreateResponse(BaseModel):
    run_id: uuid.UUID
    status: RunStatus


class RunSummary(BaseModel):
    id: uuid.UUID
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    posts_fetched: int = 0
    parse_warnings: str | None = None
    unique_vacancies: int | None = None


class RunDetail(RunSummary):
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    run_params: dict[str, Any] | None = None
    metrics: MetricsSummary | None = None
    error_message: str | None = None
    progress: dict[str, Any] | None = None
    progress_updated_at: datetime | None = None
    cancel_requested: bool = False


class VacancyListItem(BaseModel):
    """Карточка в списке: без raw/cleaned_text, только короткий preview."""

    id: str
    source_post_key: str
    source_channel: str
    source_url: str
    post_url: str | None = None
    post_date: datetime | None = None
    preview: str = Field(description="Укороченный cleaned_text для таблицы (~120 символов).")

    detected_specializations: list[str] = Field(default_factory=list)
    detected_grade: str = "unknown"
    detected_salary_min: float | None = None
    detected_salary_max: float | None = None
    detected_salary_avg: float | None = None
    detected_salary_avg_rub: float | None = None
    salary_currency: str | None = None

    work_format: str | None = None
    country: str | None = None
    city: str | None = None
    company: str | None = None
    contact: str | None = None

    technologies: list[str] = Field(default_factory=list)
    required_experience: str | None = None
    employment_type: str | None = None
    application_urls: list[str] = Field(default_factory=list)

    extraction_source: str = "rules"
    ai_confidence: float | None = None
    ai_notes: str | None = None
    vacancy_score: int = 0
    vacancy_substantive_score: int = 0

    is_duplicate: bool = False
    duplicate_group_id: str | None = None
    duplicate_reason: str | None = None

    match_score: float | None = None
    match_explanation: str | None = None
    match_stale: bool | None = Field(
        default=None,
        description="Оценка устарела (другая модель матчинга или версия промпта); None — оценки нет.",
    )

    parsed_at: datetime
    archived_at: datetime | None = None


class VacancyListResponse(BaseModel):
    items: list[VacancyListItem]
    total: int
    page: int
    page_size: int


VacancySortField = Literal[
    "post_date",
    "detected_salary_avg",
    "detected_salary_avg_rub",
    "work_format",
    "detected_grade",
    "salary_currency",
    "vacancy_score",
    "match_score",
    "resume_fit",
]
VacancySortOrder = Literal["asc", "desc"]
# Вторая ступень сортировки — только пара «дата ↔ зарплата в рублях»: под неё есть индексы.
VacancyThenSortField = Literal["post_date", "detected_salary_avg_rub"]
_SORT_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {("post_date", "detected_salary_avg_rub"), ("detected_salary_avg_rub", "post_date")}
)


def sort_pair_error(sort_by: str, then_by: str | None) -> str | None:
    """Текст ошибки для недопустимой пары ``sort_by`` + ``then_by`` или None."""
    if then_by is None or (sort_by, then_by) in _SORT_PAIRS:
        return None
    return "Вторая сортировка (then_by) доступна только для пары post_date и detected_salary_avg_rub."


class VacancyQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)
    source_channel: str | None = None
    grade: str | None = None
    specializations: list[str] = Field(
        default_factory=list,
        description="Вакансии с любой из перечисленных специализаций в detected_specializations",
    )
    technologies: list[str] = Field(
        default_factory=list,
        description="Вакансии с любой из перечисленных технологий; вместе со specializations — через И",
    )
    work_format: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    has_salary: bool | None = Field(
        default=None,
        description="True — только вакансии с рублёвой суммой зарплаты (как в аналитике), False — только без неё.",
    )
    salary_estimates: bool = Field(
        default=False,
        description=(
            "Вместе с has_salary: оценка зарплаты (vacancy_salary_estimates) считается зарплатой — "
            "true добавляет вакансии с оценкой, false убирает их. Аналитика флаг игнорирует."
        ),
    )
    q: str | None = Field(default=None, description="Поиск по cleaned_text (ILIKE)")
    include_archived: bool = Field(
        default=False,
        description="True — включить архивные вакансии (старше TTL) в выдачу.",
    )
    sort_by: VacancySortField = "post_date"
    sort_order: VacancySortOrder = "desc"
    then_by: VacancyThenSortField | None = Field(
        default=None,
        description=(
            "Вторая ступень сортировки. С sort_by=post_date дата берётся по дню (UTC): "
            "внутри дня — по зарплате. Допустима только пара post_date ↔ detected_salary_avg_rub."
        ),
    )
    then_order: VacancySortOrder = "desc"
    resume_id: int | None = Field(
        default=None,
        description="Подмешать LLM-оценку соответствия этому резюме (join vacancy_matches).",
    )
    search_mode: Literal["text", "semantic"] = Field(
        default="text",
        description=(
            "text — полнотекстовый поиск по q; semantic — по смыслу: ближайшие к q по эмбеддингу "
            "вакансии (до 300) под остальными фильтрами."
        ),
    )
    # Векторы считает роут (запрос к провайдеру), в фильтры и ключ кэша агрегатов они не входят.
    semantic_vector: list[float] | None = Field(default=None, exclude=True, repr=False)
    resume_vector: list[float] | None = Field(default=None, exclude=True, repr=False)
    # Модель, которой посчитаны векторы: сравниваются только векторы вакансий той же модели.
    embedding_model: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _check_sort_pair(self) -> VacancyQuery:
        error = sort_pair_error(self.sort_by, self.then_by)
        if error:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _split_technologies(self) -> VacancyQuery:
        # Старые ссылки и закладки: ?specialization=Python — это фильтр по технологии.
        self.specializations, self.technologies = split_specializations(self.specializations, self.technologies)
        return self


class HealthResponse(BaseModel):
    status: str
    database: bool
    config_loaded: bool


class SalaryGroupStats(BaseModel):
    """Квантили зарплаты (RUB/мес) внутри группы (грейд, формат, специализация)."""

    group: str
    count: int
    mean: float | None = None
    min: float | None = None
    p10: float | None = None
    q1: float | None = None
    median: float | None = None
    q3: float | None = None
    p90: float | None = None
    max: float | None = None


class SalaryMonthlyBucket(BaseModel):
    period: str
    count: int
    mean: float | None = None
    median: float | None = None


class SalaryPeriodGroupBucket(BaseModel):
    """Квантили зарплаты внутри пары (период × группа): год/месяц × грейд|формат."""

    period: str
    group: str
    count: int
    mean: float | None = None
    median: float | None = None
    min: float | None = None
    p10: float | None = None
    q1: float | None = None
    q3: float | None = None
    p90: float | None = None
    max: float | None = None


class SalaryDistributionBucket(BaseModel):
    range: str
    count: int


class SalaryAnalytics(BaseModel):
    by_grade: list[SalaryGroupStats] = Field(default_factory=list)
    by_work_format: list[SalaryGroupStats] = Field(default_factory=list)
    by_specialization: list[SalaryGroupStats] = Field(default_factory=list)
    monthly: list[SalaryMonthlyBucket] = Field(default_factory=list)
    yearly: list[SalaryMonthlyBucket] = Field(default_factory=list)
    by_grade_yearly: list[SalaryPeriodGroupBucket] = Field(default_factory=list)
    by_work_format_yearly: list[SalaryPeriodGroupBucket] = Field(default_factory=list)
    by_grade_monthly: list[SalaryPeriodGroupBucket] = Field(default_factory=list)
    by_work_format_monthly: list[SalaryPeriodGroupBucket] = Field(default_factory=list)
    distribution: list[SalaryDistributionBucket] = Field(default_factory=list)
    salary_display: SalaryDisplayMeta | None = None


class SourceStats(BaseModel):
    """Качество канала: доля уникальных вакансий, средний score, покрытие зарплатой."""

    channel: str
    vacancies: int = Field(description="Вакансий, впервые найденных в этом канале.")
    duplicates: int = Field(
        description="Постов канала с уже встречавшимся текстом: чужим, своим перепостом или из прошлых прогонов."
    )
    unique_share: float = Field(description="Доля постов канала с новым текстом (0–1).")
    avg_score: float | None = None
    with_salary_share: float


class SourceAnalytics(BaseModel):
    items: list[SourceStats] = Field(default_factory=list)


class CompanyStats(BaseModel):
    company: str
    vacancies: int
    avg_score: float | None = None
    with_salary: int
    avg_salary_rub: float | None = None


class CompanyAnalytics(BaseModel):
    items: list[CompanyStats] = Field(default_factory=list)


class NamedCount(BaseModel):
    name: str
    count: int


class CompanyProfileVacancy(BaseModel):
    id: str
    post_date: datetime | None = None
    specializations: list[str] = Field(default_factory=list)
    grade: str = "unknown"
    salary_rub: float | None = None
    archived: bool = False
    source_channel: str


class CompanyProfile(BaseModel):
    """Профиль компании по нормализованному названию (``company_key``)."""

    key: str
    name: str = Field(description="Самое частое написание названия.")
    variants: list[str] = Field(default_factory=list, description="Все написания, сведённые к ключу.")
    vacancies: int = Field(description="Уникальных вакансий, включая архивные.")
    active: int
    first_post: datetime | None = None
    last_post: datetime | None = None
    with_salary: int
    salary_p25: float | None = None
    salary_median: float | None = None
    salary_p75: float | None = None
    roles: list[NamedCount] = Field(default_factory=list)
    technologies: list[NamedCount] = Field(default_factory=list)
    grades: list[NamedCount] = Field(default_factory=list)
    channels: list[NamedCount] = Field(default_factory=list)
    telegram_posts: int = Field(description="Постов Telegram с вакансиями компании (включая перепосты).")
    telegram_post_days: int = Field(description="Пар «вакансия × день публикации» в Telegram.")
    recent: list[CompanyProfileVacancy] = Field(default_factory=list)
    salary_display: SalaryDisplayMeta | None = None


class VacancyFlag(BaseModel):
    code: Literal["reposted", "long_open", "frequent_role", "no_salary"]
    severity: Literal["warning", "info"]
    message: str
    value: int | None = None


class VacancyFlags(BaseModel):
    vacancy_id: str
    company_key: str | None = None
    flags: list[VacancyFlag] = Field(default_factory=list)


class RecruiterContactOut(BaseModel):
    kind: Literal["telegram", "telegram_bot", "email"]
    value: str = Field(description="Username без «@» или email.")
    url: str = Field(description="https://t.me/<username> или mailto:<email>; черновик UI добавляет сам.")
    has_contact_hint: bool = Field(description="Рядом слова отклика («Контакты», «резюме»): кандидат надёжнее.")


class RecruiterContacts(BaseModel):
    """Куда откликнуться: контакты рекрутера из текста без каналов и их подписей."""

    vacancy_id: str
    contacts: list[RecruiterContactOut] = Field(default_factory=list)
    application_urls: list[str] = Field(default_factory=list)


class MarketSalaryOut(BaseModel):
    """Рыночная вилка для вакансии: квантили рублёвой зарплаты похожих вакансий."""

    vacancy_id: str
    level: Literal["grade_role_tech", "grade_role", "grade_tech", "role"] | None = Field(
        default=None, description="Ступень критериев, на которой набралось достаточно похожих; null — не набралось."
    )
    grade: str | None = None
    specializations: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list, description="Похожая — с хотя бы одной из них.")
    count: int = 0
    p10: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    vacancy_salary: float | None = Field(default=None, description="Рублёвая зарплата самой вакансии.")
    percentile: float | None = Field(
        default=None, description="Доля похожих с зарплатой не выше, чем у вакансии (0–1)."
    )
    window_days: int
    salary_display: SalaryDisplayMeta | None = None


class SalaryEstimateOut(BaseModel):
    """Ориентировочная зарплата вакансии без вилки — по ближайшим по смыслу вакансиям с зарплатой."""

    vacancy_id: str
    status: Literal["ready", "insufficient", "pending", "has_salary", "unavailable"] = Field(
        description=(
            "ready — оценка есть; insufficient — похожих с зарплатой меньше min_neighbours; pending — у "
            "вакансии ещё нет эмбеддинга; has_salary — зарплата указана, оценка не нужна; unavailable — "
            "эмбеддинги выключены."
        )
    )
    grade: str | None = Field(default=None, description="Грейд, среди которого искали; null — среди всех.")
    neighbours: int = 0
    min_neighbours: int
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    window_days: int
    examples: list[VacancyListItem] = Field(
        default_factory=list, description="Ближайшие из похожих (до 5), по которым считали."
    )
    salary_display: SalaryDisplayMeta | None = None


class TechnologyGradeStats(BaseModel):
    """Медианы зарплаты в ₽ внутри грейда: с технологией и без неё."""

    grade: str
    with_count: int
    with_median: float | None = None
    without_count: int
    without_median: float | None = None
    compared: bool = Field(description="Грейд вошёл в расчёт премии (обе группы достаточно велики).")


class TechnologyPremium(BaseModel):
    technology: str
    vacancies: int = Field(description="Вакансий с технологией и рублёвой зарплатой.")
    median: float | None = None
    premium: float | None = Field(
        default=None,
        description=(
            "Средневзвешенное по грейдам (медиана с технологией / медиана без − 1); null — мало сопоставимых вакансий."
        ),
    )
    compared_vacancies: int = Field(description="Вакансий с технологией в грейдах, вошедших в сравнение.")
    by_grade: list[TechnologyGradeStats] = Field(default_factory=list)


class TechnologySeries(BaseModel):
    technology: str
    counts: list[int] = Field(default_factory=list, description="Вакансий с технологией по месяцам ``months``.")


class TechnologyTrend(BaseModel):
    months: list[str] = Field(default_factory=list, description="YYYY-MM по возрастанию.")
    totals: list[int] = Field(default_factory=list, description="Всего вакансий по месяцам.")
    series: list[TechnologySeries] = Field(default_factory=list)


class TechnologyMomentum(BaseModel):
    technology: str
    recent: int
    previous: int
    recent_share: float
    previous_share: float
    delta_pp: float = Field(description="Изменение доли вакансий с технологией, п.п.")


class TechnologyAnalytics(BaseModel):
    premium: list[TechnologyPremium] = Field(default_factory=list)
    trend: TechnologyTrend = Field(default_factory=TechnologyTrend)
    momentum: list[TechnologyMomentum] = Field(default_factory=list)
    momentum_window_days: int
    momentum_anchor: date | None = Field(default=None, description="Последняя дата поста под фильтрами.")
    salary_display: SalaryDisplayMeta | None = None


class TechnologyGraphNode(BaseModel):
    name: str
    count: int = Field(description="Вакансий с технологией под фильтрами.")
    share: float = Field(description="Доля вакансий под фильтрами (0–1).")
    role: str | None = Field(default=None, description="Специализация, с которой технология встречается чаще всего.")
    role_share: float = Field(default=0.0, description="Доля вакансий технологии с этой специализацией.")


class TechnologyGraphEdge(BaseModel):
    source: str
    target: str
    count: int = Field(description="Вакансий, где есть обе технологии.")
    lift: float = Field(description="Во сколько раз пара встречается чаще, чем при независимости (1 — как случайно).")


class TechnologyGraph(BaseModel):
    """Какие технологии просят вместе: узлы — частые технологии, рёбра — пары с lift."""

    total: int = Field(description="Вакансий под фильтрами.")
    nodes: list[TechnologyGraphNode] = Field(default_factory=list)
    edges: list[TechnologyGraphEdge] = Field(
        default_factory=list, description="Пары не реже min_pair_count, по убыванию lift."
    )
    min_pair_count: int


class LifetimeBucket(BaseModel):
    label: str
    min_days: int
    max_days: int | None = Field(default=None, description="Включительно; null — без верхней границы.")
    count: int


class LifetimeGroup(BaseModel):
    """Жизнь вакансий в группе (роль или грейд)."""

    name: str
    vacancies: int
    republished: int = Field(description="Публиковали в 2+ разных дня.")
    republished_share: float
    long_open: int = Field(description="Между первой и последней публикацией не меньше ``long_open_days``.")
    median_span_days: float | None = Field(default=None, description="Медиана срока у повторно публикуемых.")


class LifetimeCompany(BaseModel):
    company: str
    vacancies: int
    republished: int
    long_open: int
    max_span_days: int


class LifetimeVacancy(BaseModel):
    id: str
    company: str | None = None
    specializations: list[str] = Field(default_factory=list)
    grade: str = "unknown"
    source_channel: str
    first_at: datetime
    last_at: datetime
    span_days: int
    publication_days: int
    posts: int
    archived: bool = False


class LifetimeAnalytics(BaseModel):
    """Сколько вакансия остаётся на рынке по публикациям в Telegram (HH не входит)."""

    vacancies: int = Field(description="Вакансий Telegram под фильтрами (с хотя бы одной публикацией).")
    republished: int
    republished_share: float
    long_open: int
    long_open_days: int
    span_p50_days: float | None = Field(default=None, description="Медиана срока у повторно публикуемых.")
    span_p90_days: float | None = None
    buckets: list[LifetimeBucket] = Field(default_factory=list)
    by_role: list[LifetimeGroup] = Field(default_factory=list)
    by_grade: list[LifetimeGroup] = Field(default_factory=list)
    companies: list[LifetimeCompany] = Field(default_factory=list)
    longest: list[LifetimeVacancy] = Field(default_factory=list)
    min_group: int = Field(description="Роль или грейд показываются от стольких вакансий.")


# Имя обрезается до проверки длины: иначе «   » проходило min_length и сохранялось пустым.
UserItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SavedFilterCreate(BaseModel):
    name: UserItemName
    params: VacancyQuery = Field(default_factory=VacancyQuery)


class SavedFilterOut(BaseModel):
    id: int
    name: str
    params: VacancyQuery
    created_at: datetime


class SavedFilterListResponse(BaseModel):
    items: list[SavedFilterOut] = Field(default_factory=list)


# Резюме целиком уходит в промпт матчинга и письма: без потолка один запрос
# раздувает токены (и стоимость) каждого вызова.
RESUME_TEXT_MAX_LENGTH = 50_000
# Текст одной вакансии в ручном импорте (пост Telegram — до 4096 символов, HH — длиннее).
IMPORT_TEXT_MAX_LENGTH = 30_000


class ResumeCreate(BaseModel):
    name: UserItemName
    text: str = Field(min_length=1, max_length=RESUME_TEXT_MAX_LENGTH)


class ResumeUpdate(BaseModel):
    name: UserItemName | None = None
    text: str | None = Field(default=None, min_length=1, max_length=RESUME_TEXT_MAX_LENGTH)


class ResumeOut(BaseModel):
    id: int
    name: str
    text: str
    created_at: datetime


class ResumeListResponse(BaseModel):
    items: list[ResumeOut] = Field(default_factory=list)


class VacancyMatchOut(BaseModel):
    """Оценка одной пары резюме↔вакансия (соло-матчинг из карточки вакансии)."""

    resume_id: int
    vacancy_id: str
    overall_score: int = Field(description="0-100: итоговый score, считает backend.")
    confidence: int = Field(description="0-100: уверенность LLM в качестве оценки, не сам score.")
    breakdown: MatchBreakdown
    explanation: str | None = None
    matching_skills: list[SkillMatch] = Field(default_factory=list)
    missing_skills: list[SkillMatch] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    model: str
    created_at: datetime
    cached: bool = Field(
        default=False,
        description="True — оценка взята из БД, без нового вызова LLM.",
    )
    stale: bool = Field(
        default=False,
        description=(
            "True — оценка получена при другой настроенной модели матчинга или версии промпта; "
            "POST без force пересчитает её."
        ),
    )


class ResumeTailoringOut(BaseModel):
    """Адаптация резюме под вакансию: правки реального опыта под требования, без выдумок."""

    resume_id: int
    vacancy_id: str
    summary: str
    suggestions: list[ResumeTailoringSuggestion] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list, description="Термины вакансии, которые резюме подтверждает.")
    do_not_claim: list[str] = Field(
        default_factory=list, description="Требования, которых в резюме нет: не добавлять без реального опыта."
    )
    model: str
    created_at: datetime
    cached: bool = Field(default=False, description="True — из БД, без нового вызова LLM.")
    stale: bool = Field(
        default=False,
        description="Получено другой моделью (как у писем) или версией промпта; POST без force пересчитает.",
    )


class InterviewPrepOut(BaseModel):
    """Подготовка к собеседованию для пары резюме↔вакансия (LLM)."""

    resume_id: int
    vacancy_id: str
    summary: str = ""
    questions: list[InterviewQuestion] = Field(default_factory=list)
    gaps: list[InterviewGap] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)
    model: str
    created_at: datetime
    cached: bool = False
    stale: bool = Field(
        default=False,
        description="Получено другой моделью (как у писем) или версией промпта; POST без force пересчитает.",
    )


class ResumeFileTextOut(BaseModel):
    """Текст резюме, извлечённый из загруженного файла (в базу не сохраняется)."""

    text: str
    format: Literal["pdf", "docx"]
    chars: int
    truncated: bool = Field(description=f"Текст длиннее {RESUME_TEXT_MAX_LENGTH} символов обрезан.")
    pages: int | None = None


class SkillGapItem(BaseModel):
    """Недостающий навык в сводке по оценкам резюме."""

    name: str
    vacancies: int = Field(description="Разных оценённых вакансий с этим пробелом.")
    critical: int = 0
    important: int = 0
    secondary: int = 0
    share: float = Field(description="vacancies / evaluated.")
    vacancy_ids: list[str] = Field(default_factory=list, description="До трёх примеров, свежие оценки первыми.")
    market_vacancies: int | None = Field(
        default=None,
        description="Только у технологий: активных уникальных вакансий каталога с этой технологией.",
    )


class SkillGapsOut(BaseModel):
    """Сводка недостающих навыков по всем оценкам резюме."""

    resume_id: int
    evaluated: int
    technologies: list[SkillGapItem] = Field(default_factory=list)
    other: list[SkillGapItem] = Field(default_factory=list)


class MarketShare(BaseModel):
    """Значение и доля ближайших к резюме вакансий, где оно встречается."""

    name: str
    share: float = Field(description="Доля ближайших вакансий (0–1).")


class ResumeMarketTechnology(MarketShare):
    in_resume: bool = Field(description="Технология названа в тексте резюме.")


class ResumeMarketOut(BaseModel):
    """Резюме против рынка: ближайшие к резюме вакансии по эмбеддингу (без LLM)."""

    resume_id: int
    window_days: int
    peers: int = Field(description="Сколько ближайших вакансий за окно взято для профиля и технологий.")
    grades: list[MarketShare] = Field(default_factory=list, description="Грейды ближайших, частые первыми.")
    specializations: list[MarketShare] = Field(default_factory=list, description="Роли ближайших.")
    salary_count: int = Field(default=0, description="Ближайших вакансий с рублёвой зарплатой в квантилях.")
    p10: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    technologies: list[ResumeMarketTechnology] = Field(
        default_factory=list, description="Частые технологии ближайших и есть ли они в резюме."
    )
    coverage: float | None = Field(
        default=None,
        description="В среднем по ближайшим с технологиями: доля технологий вакансии, названных в резюме.",
    )
    resume_technologies: list[str] = Field(default_factory=list, description="Технологии, найденные в резюме.")
    salary_display: SalaryDisplayMeta | None = None


class SkillWhatIfOption(BaseModel):
    technology: str
    demand: int = Field(description="Вакансий сегмента, где технология нужна, а в резюме её нет.")
    demand_share: float = Field(description="Доля от вакансий сегмента с технологиями.")
    new_fit: int = Field(description="Сколько вакансий станут подходящими, если добавить технологию.")
    coverage_gain: float = Field(description="Рост среднего покрытия технологий вакансий сегмента (доля, 0–1).")
    premium: float | None = Field(
        default=None, description="Зарплатная премия технологии (медиана с / медиана без − 1); null — мало данных."
    )
    premium_scope: Literal["grade", "role"] | None = Field(
        default=None, description="grade — внутри грейда резюме; role — средневзвешенно по грейдам роли."
    )
    median_with: float | None = Field(default=None, description="Медиана ₽ вакансий сегмента с технологией.")


class SkillWhatIfPeer(BaseModel):
    """Вакансия сегмента для подсчёта сочетаний в UI: всего технологий, покрыто резюме, недостающие из вариантов."""

    total: int
    covered: int
    missing: list[str] = Field(default_factory=list)


class SkillWhatIfOut(BaseModel):
    """«Что если выучить»: эффект недостающих технологий для сегмента рынка резюме (без LLM)."""

    resume_id: int
    window_days: int
    peers: int = Field(description="Ближайших к резюме вакансий за окно.")
    with_technologies: int = Field(default=0, description="Из них с технологиями — по ним считается покрытие.")
    role: str | None = Field(default=None, description="Самая частая роль сегмента — рынок для премии.")
    grade: str | None = Field(default=None, description="Самый частый грейд сегмента.")
    fit_threshold: float
    baseline_fit: int = 0
    baseline_coverage: float | None = None
    resume_technologies: list[str] = Field(default_factory=list)
    options: list[SkillWhatIfOption] = Field(default_factory=list)
    peer_gaps: list[SkillWhatIfPeer] = Field(default_factory=list)
    salary_display: SalaryDisplayMeta | None = None


class CareerSegment(BaseModel):
    """Вакансии роли (в грейде или во всех грейдах): насколько резюме им подходит и что платят."""

    role: str
    grade: str | None = Field(default=None, description="Грейд сегмента; null — все грейды роли.")
    vacancies: int = Field(description="Вакансий сегмента с технологиями — по ним покрытие и доля подходящих.")
    fit_share: float = Field(description="Доля вакансий сегмента, которым резюме подходит уже сейчас.")
    coverage: float = Field(description="Сколько технологий вакансии в среднем названо в резюме (доля, 0–1).")
    salary_median: float | None = Field(default=None, description="Медиана ₽ вакансий сегмента; null — мало данных.")
    salary_count: int = 0


class CareerRouteStep(BaseModel):
    technology: str
    demand_share: float = Field(description="Доля вакансий сегмента, где технология нужна, а в резюме её нет.")
    fit_share: float = Field(description="Доля подходящих вакансий после этого шага (с предыдущими).")
    coverage: float = Field(description="Среднее покрытие технологий после этого шага (с предыдущими).")


class CareerPath(CareerSegment):
    kind: Literal["grade", "role"] = Field(description="grade — следующий грейд своей роли; role — соседняя роль.")
    closeness: float | None = Field(
        default=None, description="Доля роли среди ближайших к резюме вакансий; null — у шага по грейду."
    )
    route: list[CareerRouteStep] = Field(default_factory=list)
    salary_uplift: float | None = Field(
        default=None, description="Медиана цели / медиана текущей позиции − 1; null — мало данных."
    )


class CareerPathsOut(BaseModel):
    """Карьерная траектория: следующий грейд и соседние роли с маршрутом технологий (без LLM)."""

    resume_id: int
    window_days: int
    peers: int = Field(description="Ближайших к резюме вакансий, по которым определены позиция и соседние роли.")
    fit_threshold: float
    resume_technologies: list[str] = Field(default_factory=list)
    current: CareerSegment | None = None
    paths: list[CareerPath] = Field(default_factory=list)
    salary_display: SalaryDisplayMeta | None = None


class MarketMapCluster(BaseModel):
    """Ниша на карте рынка: подпись без LLM, состав, зарплата и рост за 30 дней."""

    id: int
    label: str
    size: int
    x: float
    y: float
    roles: list[MarketShare] = Field(default_factory=list)
    technologies: list[MarketShare] = Field(default_factory=list)
    salary_median: float | None = None
    salary_count: int = 0
    recent: int = Field(default=0, description="Вакансий ниши за последние 30 дней карты.")
    previous: int = Field(default=0, description="За 30 дней до них.")
    delta_pp: float | None = Field(default=None, description="Изменение доли ниши среди вакансий окна, п.п.")


class MarketMapResume(BaseModel):
    resume_id: int
    x: float
    y: float
    cluster: int
    nearest: list[str] = Field(default_factory=list, description="Ближайшие к резюме вакансии карты.")


class MarketMapOut(BaseModel):
    """Карта рынка: точки колонками (id, x, y, ниша, роль, грейд, зарплата, компания, архив) и ниши."""

    available: bool
    created_at: datetime | None = None
    model: str | None = None
    points: int = 0
    window_days: int | None = None
    clusters: list[MarketMapCluster] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)
    x: list[float] = Field(default_factory=list)
    y: list[float] = Field(default_factory=list)
    cluster: list[int] = Field(default_factory=list)
    role: list[str | None] = Field(default_factory=list)
    grade: list[str | None] = Field(default_factory=list)
    salary: list[float | None] = Field(default_factory=list)
    company: list[str | None] = Field(default_factory=list)
    archived: list[bool] = Field(default_factory=list)
    resume: MarketMapResume | None = None
    resume_error: str | None = None
    # Администратор запросил пересчёт, фоновое обслуживание его ещё не выполнило.
    rebuild_pending: bool = False


CoverLetterTone = Literal["neutral", "friendly", "formal"]


class CoverLetterRequest(BaseModel):
    """Параметры генерации сопроводительного письма (тело POST)."""

    tone: CoverLetterTone = "neutral"
    extra_notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Свободные пожелания к письму (акценты, что упомянуть).",
    )


class CoverLetterOut(BaseModel):
    """Сгенерированное письмо для пары резюме↔вакансия."""

    resume_id: int
    vacancy_id: str
    text: str
    tone: str
    model: str
    created_at: datetime
    cached: bool = Field(
        default=False,
        description="True — текст взят из БД, без нового вызова LLM.",
    )
    stale: bool = Field(
        default=False,
        description=(
            "True — письмо получено при другой настроенной модели писем или версии промпта "
            "(тон не учитывается); POST без force перегенерирует его."
        ),
    )


class AskMarketTurn(BaseModel):
    """Прошлый вопрос диалога «Спросить базу»: для уточнений («а для senior?»)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=500)
    answer: str = Field(default="", max_length=4000)
    filters: dict[str, Any] = Field(default_factory=dict, description="Фильтры, по которым считали ответ.")


class AskMarketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    history: list[AskMarketTurn] = Field(default_factory=list, max_length=3)


class AskMarketVariant(BaseModel):
    label: str
    filters: dict[str, Any] = Field(default_factory=dict)


class AskMarketResult(BaseModel):
    """Ответ «Спросить базу»: фильтры, по которым считали, цифры и текст модели по ним."""

    question: str
    answerable: bool
    answer: str = ""
    filters: dict[str, Any] = Field(default_factory=dict, description="Поля VacancyQuery (без пустых).")
    topics: list[str] = Field(default_factory=list)
    variants: list[AskMarketVariant] = Field(default_factory=list)
    unrecognized: list[str] = Field(default_factory=list, description="Роли, грейды, форматы из вопроса не из словаря.")
    facts: dict[str, Any] | None = None
    model: str | None = None
    prompt_version: str | None = None


class LlmJobAccepted(BaseModel):
    """Ответ 202: LLM-задача поставлена в очередь."""

    job_id: uuid.UUID
    status: str = "queued"


class LlmJobOut(BaseModel):
    """Статус фоновой LLM-задачи; при done — готовый match или cover_letter.

    Импорт (``kind=import``): резюме и вакансии нет, ``import_result`` — итог по
    уже обработанным текстам (растёт, пока джоба ``running``). Переразбор
    (``kind=reextract``) — так же, итог в ``reextract_result``.
    """

    id: uuid.UUID
    kind: str
    status: str
    resume_id: int | None = None
    vacancy_id: str | None = None
    error_message: str | None = None
    match: VacancyMatchOut | None = None
    cover_letter: CoverLetterOut | None = None
    resume_tailoring: ResumeTailoringOut | None = None
    interview_prep: InterviewPrepOut | None = None
    import_result: VacancyImportResponse | None = None
    reextract_result: VacancyReextractResponse | None = None
    match_batch_result: MatchBatchResponse | None = None
    ask_market_result: AskMarketResult | None = None
    cancel_requested: bool = Field(
        default=False,
        description="Запрошена отмена; у running — воркер остановит задачу в ближайшей точке проверки.",
    )
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class VacancyImportRequest(BaseModel):
    """Ручной импорт: тексты вакансий (например, скопированные с HH) + опциональная ссылка."""

    texts: list[Annotated[str, Field(max_length=IMPORT_TEXT_MAX_LENGTH)]] = Field(min_length=1, max_length=50)
    source_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Ссылка на оригинал (hh.ru и т.п.) — сохраняется как post_url.",
    )


VacancyImportStatus = Literal["created", "exists", "not_vacancy", "error"]


class VacancyImportItemResult(BaseModel):
    index: int
    status: VacancyImportStatus
    message: str | None = None
    vacancy: Vacancy | None = None


class VacancyImportResponse(BaseModel):
    items: list[VacancyImportItemResult]
    created: int
    existing: int
    rejected: int
    ai_enabled: bool
    warning: str | None = None
    # Текстов в запросе: у выполняемой джобы импорта items короче — это прогресс.
    total: int = 0


VacancyReextractStatus = Literal["updated", "kept", "error"]


class VacancyReextractItem(BaseModel):
    index: int
    vacancy_id: str
    status: VacancyReextractStatus
    message: str | None = None


class VacancyReextractResponse(BaseModel):
    """Итог переразбора LLM: по вакансиям, растёт, пока джоба ``running``."""

    total: int = 0
    items: list[VacancyReextractItem] = Field(default_factory=list)
    updated: int = 0
    kept: int = 0
    errors: int = 0
    warning: str | None = None


MatchBatchStatus = Literal["scored", "skipped", "error"]


class MatchBatchItem(BaseModel):
    index: int
    vacancy_id: str
    status: MatchBatchStatus
    score: int | None = None
    message: str | None = None


class MatchBatchResponse(BaseModel):
    """Итог пакетной оценки: по вакансиям, растёт, пока джоба ``running``."""

    total: int = 0
    items: list[MatchBatchItem] = Field(default_factory=list)
    scored: int = 0
    skipped: int = Field(default=0, description="Свежая оценка уже была — LLM не вызывался.")
    errors: int = 0
    warning: str | None = None


class MatchBatchRequest(BaseModel):
    vacancy_ids: list[str] = Field(min_length=1, max_length=20)


class SimilarVacancy(BaseModel):
    vacancy: VacancyListItem
    similarity: float = Field(description="1 − косинусное расстояние эмбеддингов (1 — тот же смысл).")


class SimilarVacanciesResponse(BaseModel):
    items: list[SimilarVacancy] = Field(default_factory=list)
    pending: bool = Field(default=False, description="У вакансии ещё нет эмбеддинга — индексация впереди.")


class EmbeddingStatus(BaseModel):
    enabled: bool
    model: str
    vacancies: int = Field(description="Уникальных вакансий в базе (с архивом).")
    embedded: int = Field(description="Из них с эмбеддингом текущей модели.")


class VacancyReextractInfo(BaseModel):
    """Сколько вакансий без специализации ждут переразбора и активная джоба, если есть."""

    missing_role: int
    max_per_job: int
    ai_enabled: bool
    active_job_id: uuid.UUID | None = None


class TechnologyCount(BaseModel):
    name: str
    count: int


class TechnologyFacetResponse(BaseModel):
    """Технологии по числу вакансий — варианты фильтра списка."""

    items: list[TechnologyCount] = Field(default_factory=list)


LlmJobOut.model_rebuild()


class AuthCredentials(BaseModel):
    """Вход: email не валидируется — пользователи, заведённые до проверки формата, входят как раньше."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=256)


class RegisterRequest(AuthCredentials):
    email: EmailStr = Field(max_length=320)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class UserOut(BaseModel):
    id: int
    email: str
    is_admin: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="TTL access-токена в секундах")
    user: UserOut


class SseTicketResponse(BaseModel):
    ticket: str = Field(description="Одноразовый тикет для ?ticket= SSE-эндпоинта")
    expires_in: int = Field(description="Сколько секунд тикет можно предъявить")


ApiTokenScope = Literal["read", "llm"]


class ApiTokenCreate(BaseModel):
    """Новый персональный токен доступа: ``read`` обязателен, ``llm`` — разрешить LLM-задачи."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    scopes: list[ApiTokenScope] = Field(default_factory=lambda: ["read"], min_length=1)
    expires_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1, le=MAX_TTL_DAYS)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Имя токена не может быть пустым")
        return v

    @field_validator("scopes")
    @classmethod
    def _needs_read(cls, v: list[str]) -> list[str]:
        if "read" not in v:
            raise ValueError("Scope read обязателен: без него токен ничего не может прочитать")
        return v


class ApiTokenOut(BaseModel):
    id: int
    name: str
    prefix: str = Field(description="Начало токена — чтобы узнать его в списке.")
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None


class ApiTokenCreated(ApiTokenOut):
    token: str = Field(description="Значение токена — показывается только в этом ответе.")


class DigestCount(BaseModel):
    """Число вакансий недели с признаком и за прошлую неделю."""

    name: str
    count: int
    previous: int = 0


class DigestSalary(BaseModel):
    """Медиана рублёвой зарплаты за 28 дней до конца недели и за 28 дней перед ними."""

    grade: str = Field(description="Грейд или «all» — все грейды.")
    median: float | None = None
    count: int = 0
    previous_median: float | None = None
    previous_count: int = 0


class DigestTechnology(BaseModel):
    """Доля вакансий недели с технологией против доли за 4 недели до неё."""

    name: str
    share: float
    previous_share: float
    delta_pp: float = Field(description="Изменение доли в процентных пунктах.")


class DigestFacts(BaseModel):
    """Посчитанные факты недели — вход LLM и данные страницы обзора."""

    period_start: date
    period_end: date
    vacancies: int = Field(description="Уникальных вакансий с датой поста в неделе (с архивом).")
    previous_vacancies: int = Field(description="То же за прошлую неделю.")
    avg_4w_vacancies: float = Field(description="Среднее в неделю за 4 недели до этой.")
    with_salary_share: float | None = Field(default=None, description="Доля вакансий недели с рублёвой зарплатой.")
    roles: list[DigestCount] = Field(default_factory=list)
    work_formats: list[DigestCount] = Field(default_factory=list)
    salaries: list[DigestSalary] = Field(default_factory=list)
    salary_window_days: int = 28
    technologies_up: list[DigestTechnology] = Field(default_factory=list)
    technologies_down: list[DigestTechnology] = Field(default_factory=list)
    top_companies: list[DigestCount] = Field(default_factory=list)
    new_companies: list[DigestCount] = Field(
        default_factory=list, description="Компании, чья первая вакансия в базе — на этой неделе."
    )
    sources: list[DigestCount] = Field(default_factory=list, description="Каналы и HH с числом вакансий недели.")


class DigestListItem(BaseModel):
    period_start: date
    period_end: date
    vacancies: int
    has_summary: bool
    created_at: datetime


class DigestListResponse(BaseModel):
    items: list[DigestListItem] = Field(default_factory=list)


class DigestOut(BaseModel):
    """Обзор рынка за неделю: факты и текст LLM по ним."""

    period_start: date
    period_end: date
    facts: DigestFacts
    summary: str | None = None
    summary_error: str | None = Field(default=None, description="Почему нет текста LLM (факты при этом есть).")
    model: str | None = None
    created_at: datetime
    stale: bool = Field(default=False, description="Текст получен другой моделью или версией промпта.")


class DigestGenerateRequest(BaseModel):
    week_start: date | None = Field(
        default=None, description="Понедельник недели (UTC); по умолчанию — прошедшая неделя."
    )
    force: bool = Field(default=False, description="Пересчитать, даже если обзор за неделю уже есть.")


class ChannelCandidate(BaseModel):
    """Канал, из которого пересылают посты в наши каналы, — кандидат в источники."""

    channel_id: int
    username: str
    title: str | None = None
    status: Literal["new", "ignored"] = "new"
    forwards: int = Field(description="Разных пересланных постов в наших каналах.")
    vacancy_forwards: int = Field(description="Из них прошли как вакансии (правила/LLM и фильтры).")
    seen_in: list[str] = Field(default_factory=list, description="Наши каналы, куда пересылают.")
    first_post_date: datetime | None = None
    last_post_date: datetime | None = None
    example_url: str | None = Field(default=None, description="Последний пересланный пост в исходном канале.")


class ChannelCandidatesResponse(BaseModel):
    items: list[ChannelCandidate] = Field(default_factory=list)


class ChannelCandidatePatch(BaseModel):
    status: Literal["new", "ignored"]


class VacancyChange(BaseModel):
    """Отличие перепоста от первой публикации."""

    field: Literal["salary", "work_format", "city", "technologies"]
    old: Any = None
    new: Any = None
    added: list[str] = Field(default_factory=list, description="Только у technologies.")
    removed: list[str] = Field(default_factory=list, description="Только у technologies.")


class VacancySightingOut(BaseModel):
    """Появление вакансии: первая публикация, перепост того же текста или перепост с правками."""

    source_channel: str
    post_date: datetime | None = None
    post_url: str | None = None
    kind: Literal["original", "repost", "edited"]
    duplicate_reason: str | None = None
    changes: list[VacancyChange] = Field(default_factory=list)


class VacancyHistory(BaseModel):
    vacancy_id: str
    duplicate_of: str | None = Field(
        default=None, description="Вакансия скрыта как копия этой (разовое склеивание почти-дублей)."
    )
    sightings: list[VacancySightingOut] = Field(default_factory=list)
    edits: int = Field(default=0, description="Перепостов с изменёнными полями.")
