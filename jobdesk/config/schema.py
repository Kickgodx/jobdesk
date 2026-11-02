"""Pydantic-схема config.yaml и встроенные словари по умолчанию."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jobdesk.ai.models_catalog import LlmModelEntry

# Значения OpenRouter ``reasoning.effort`` (по убыванию).
REASONING_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")


DEFAULT_SPECIALIZATIONS: dict[str, list[str]] = {
    # Ручное тестирование и «тестировщик» — тот же QA; отдельных Manual QA / Тестировщик нет
    # (миграция 036). Старые имена приводит SPECIALIZATION_ALIASES.
    "QA": [
        "qa",
        "quality assurance",
        "тестировщик",
        "тестирование",
        "tester",
        "manual qa",
        "ручной тестировщик",
    ],
    "Automation QA": [
        "automation qa",
        "qa automation",
        "aqa",
        "автотестировщик",
        "автоматизатор тестирования",
    ],
    "SDET": ["sdet", "software development engineer in test"],
    # Языки (Python, Java, Go…) — не специализации, а технологии: DEFAULT_TECHNOLOGIES
    # и split_specializations (миграция 037).
    "Backend": ["backend", "бэкенд", "бэкэнд", "бэк-енд"],
    "DevOps": ["devops", "dev ops", "девопс"],
    "Data Engineer": ["data engineer", "дата инженер", "инженер данных"],
    "AI": [
        "ai engineer",
        "ai developer",
        "ai specialist",
        "ai/ml",
        "ai разработчик",
        "machine learning engineer",
        "ml engineer",
        "llm engineer",
        "llm developer",
        "prompt engineer",
        "nlp engineer",
        "computer vision engineer",
        "computer vision",
        "deep learning engineer",
        "искусственный интеллект",
        "инженер ии",
        "разработчик ии",
        "нейросет",
        "mlops engineer",
    ],
    "Android": ["android", "андроид"],
    "iOS": ["ios", "swift developer", "apple developer"],
    "Software Engineer": [
        "software engineer",
        "software developer",
        "инженер-программист",
        "программист",
    ],
    "Frontend": ["frontend", "front-end", "front end", "фронтенд", "react developer"],
    "Fullstack": ["fullstack", "full-stack", "full stack", "фулстек"],
    "Analyst": ["analyst", "аналитик"],
    "System Analyst": ["system analyst", "системный аналитик", "са"],
    "Business Analyst": ["business analyst", "бизнес-аналитик", "ба"],
    "Product Manager": ["product manager", "продакт", "продакт-менеджер", "pm"],
    "Project Manager": ["project manager", "проектный менеджер", "пм"],
}


# Прежние имена специализаций → каноническое (LLM, кэш extraction, config.yaml, URL фильтров).
SPECIALIZATION_ALIASES: dict[str, str] = {
    "Manual QA": "QA",
    "Тестировщик": "QA",
    "AQA": "Automation QA",
    "Программист, разработчик": "Software Engineer",
}


def canonical_specializations(names: list[str]) -> list[str]:
    """Имена через ``SPECIALIZATION_ALIASES``, без повторов, порядок сохраняется."""
    out: list[str] = []
    for raw in names:
        name = raw.strip()
        name = SPECIALIZATION_ALIASES.get(name, name)
        if name and name not in out:
            out.append(name)
    return out


DEFAULT_EXCLUDE_KEYWORDS = [
    "junior",
    "intern",
    "internship",
    "стажировка",
    "без опыта",
    "обучение",
    "курсы",
    "реклама",
    "ищу работу",
    "резюме",
    "hr",
    "sales",
    "продажник",
    "не it",
]


# Словарь rule-based поиска технологий по тексту поста (и канонические имена для
# нормализации). Однобуквенные и двусмысленные имена (C, Unity) сюда не входят — их
# узнаёт только _EXTRA_TECHNOLOGY_NAMES, когда LLM ставит их специализацией.
DEFAULT_TECHNOLOGIES = [
    "Python",
    "Java",
    "Kotlin",
    "Swift",
    "Go",
    "JavaScript",
    "TypeScript",
    "PHP",
    "C++",
    "C#",
    ".NET",
    "Rust",
    "Ruby",
    "Scala",
    "Node.js",
    "1С",
    "SQL",
    "React",
    "Vue",
    "Angular",
    "Flutter",
    "Django",
    "FastAPI",
    "Flask",
    "Spring Boot",
    "Spring",
    "Kafka",
    "RabbitMQ",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Ansible",
    "PostgreSQL",
    "MySQL",
    "Redis",
    "MongoDB",
    "ClickHouse",
    "Elasticsearch",
    "Airflow",
    "Spark",
    "PyTorch",
    "TensorFlow",
    "Selenium",
    "Playwright",
    "Pytest",
    "Appium",
    "REST",
    "GraphQL",
    "gRPC",
    "Linux",
    "AWS",
    "GCP",
    "Azure",
]

# Технологии, которые узнаются в специализациях от LLM, но не ищутся по тексту.
_EXTRA_TECHNOLOGY_NAMES = [
    "C",
    "Objective-C",
    "Dart",
    "Elixir",
    "Erlang",
    "Haskell",
    "Clojure",
    "Perl",
    "Lua",
    "Delphi",
    "Solidity",
    "Bitrix",
    "Laravel",
    "Symfony",
    "Ruby on Rails",
    "Next.js",
    "Svelte",
    "React Native",
    "Unity",
    "Unreal Engine",
    "Godot",
]

# Варианты написания (lower) → каноническое имя технологии.
TECHNOLOGY_ALIASES: dict[str, str] = {
    "golang": "Go",
    "go lang": "Go",
    "go-lang": "Go",
    "python3": "Python",
    "python 3": "Python",
    "postgres": "PostgreSQL",
    "postgre": "PostgreSQL",
    "postgre sql": "PostgreSQL",
    "apache kafka": "Kafka",
    "apache airflow": "Airflow",
    "apache spark": "Spark",
    "rest api": "REST",
    "rest-api": "REST",
    "restful": "REST",
    "restful api": "REST",
    "js": "JavaScript",
    "ts": "TypeScript",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "k8s": "Kubernetes",
    "dotnet": ".NET",
    ".net core": ".NET",
    "c#/.net": ".NET",
    "c# .net": ".NET",
    "c#.net": ".NET",
    "1c": "1С",
    "1c:предприятие": "1С",
    "1с:предприятие": "1С",
    "reactjs": "React",
    "react.js": "React",
    "vuejs": "Vue",
    "vue.js": "Vue",
    "angularjs": "Angular",
    "spring framework": "Spring",
    "elastic": "Elasticsearch",
    "elastic search": "Elasticsearch",
    "ror": "Ruby on Rails",
    "nextjs": "Next.js",
}

_TECHNOLOGY_CATALOG: dict[str, str] = {t.lower(): t for t in DEFAULT_TECHNOLOGIES + _EXTRA_TECHNOLOGY_NAMES}

# «Python Developer», «Go-разработчик», «Программист 1С» — роль «разработчик на технологии».
_DEV_WORDS = r"(?:developer|engineer|dev|разработчик|программист)"
_TECH_ROLE_RE = re.compile(
    rf"^(?P<tech>.+?)[\s\-]+{_DEV_WORDS}$|^{_DEV_WORDS}[\s\-]+(?P<tech2>.+)$",
    re.IGNORECASE,
)
# Роль, если LLM назвал только «<язык> developer» без функции (backend, frontend…).
GENERIC_DEVELOPER_ROLE = "Software Engineer"


def technology_name(name: str) -> str | None:
    """Каноническое имя, если ``name`` — известная технология (каталог или алиас), иначе None."""
    key = " ".join(name.strip().lower().split())
    if not key:
        return None
    return TECHNOLOGY_ALIASES.get(key) or _TECHNOLOGY_CATALOG.get(key)


# Любое упоминание 1С как отдельного токена («1С:Предприятие 8.3», «1С КА», «язык запросов 1С»,
# латинская «1C») — одна технология 1С. «1С-Битрикс» — отдельный продукт (Bitrix).
_ONE_C_RE = re.compile(r"(?<![0-9a-zа-яё])1[сc](?![0-9a-zа-яё])", re.IGNORECASE)
_BITRIX_RE = re.compile(r"битрикс|bitrix", re.IGNORECASE)


def _technology_family(name: str) -> str | None:
    """Семейство технологии по вхождению (только для списка технологий, не для ролей)."""
    if _BITRIX_RE.search(name):
        return "Bitrix"
    if _ONE_C_RE.search(name):
        return "1С"
    return None


def canonical_technologies(names: list[str]) -> list[str]:
    """Технологии через алиасы и регистр каталога («golang» → Go); без повторов, порядок сохраняется.

    Варианты 1С («1С:Предприятие 8.3», «1С КА») сводятся к 1С. Неизвестные имена остаются
    как есть — словарь LLM открытый.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split())
        if not name:
            continue
        name = technology_name(name) or _technology_family(name) or name
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


# Чистка технологий из открытых источников (key_skills HH, ответ LLM) — при записи вакансии,
# не в ``canonical_technologies``: та применяется и к фильтрам пользователя.
# Русские слова без латиницы и цифр («Функциональное тестирование», «Английский язык»,
# «Коммуникабельность») — практики и навыки, а не технологии; аббревиатуры (ООП, СКД, ЗУП) — оставляем.
_CYRILLIC_ONLY_RE = re.compile(r"^[а-яё\-]+(?:\s+[а-яё\-]+)*$", re.IGNORECASE)
# Требование фразой: «Опыт работы с …», «Знание …», «Навык написания …».
_SENTENCE_RE = re.compile(
    r"^(?:опыт|знани|навык|умени|понимани|владени|готовност|желательно|участи|"
    r"experience|knowledge|understanding|ability)",
    re.IGNORECASE,
)
# Список в одном навыке: «Python, FastAPI; Kafka», «UWP / Avalonia / Qt», «C# или C++»,
# «pytest (или unittest)». C#/.NET без пробелов — одно имя.
# Английское «and» не делит: «Identity and Access Management» — один термин.
_LIST_SPLIT_RE = re.compile(r"\s*[,;()]\s*|\s+/\s+|\s+(?:или|либо|or|и)\s+", re.IGNORECASE)
# Союз в начале части после скобки: «pytest (или unittest)» → «или unittest».
_LEADING_CONJUNCTION_RE = re.compile(r"^(?:или|либо|or|и)\s+", re.IGNORECASE)
# Подпись категории: «Monitoring: Sentry», «САПР — Altium» — разбирается то, что после неё.
_LABEL_RE = re.compile(r"^[^:—–]{1,30}?(?::|\s[—–]\s)\s*")
_PARENS_RE = re.compile(r"\(([^()]*)\)")
# Версия в конце: «Python 3.13+», «Vue 3», «Angular v17» — отрезается, только если остаётся
# известная технология: в «ISO 27001», «OWASP Top 10», «SOC 2» число — часть названия.
_VERSION_TAIL_RE = re.compile(r"\s+v?\d+(?:\.\d+)*\+?$", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
# Поиск технологий каталога внутри фразы: длинные имена раньше («Spring Boot» до «Spring»);
# однобуквенные (C, R) в тексте неотличимы от букв.
_CATALOG_SCAN: list[tuple[str, str]] = sorted(
    ((k, v) for k, v in {**_TECHNOLOGY_CATALOG, **TECHNOLOGY_ALIASES}.items() if len(k) > 1),
    key=lambda kv: -len(kv[0]),
)
_MAX_TECH_WORDS = 4
_MAX_TECH_CHARS = 40


def _is_practice(name: str) -> bool:
    if not _CYRILLIC_ONLY_RE.match(name):
        return False
    return " " in name or not name.isupper()


def _known_technology(name: str) -> str | None:
    return technology_name(name) or _technology_family(name)


def _catalog_mentions(text: str) -> list[str]:
    """Технологии каталога, названные в тексте фразы целым словом («…на Python/Java с Kafka»)."""
    found: list[tuple[int, str]] = []
    for key, canon in _CATALOG_SCAN:
        m = re.search(rf"(?<![\w.+#]){re.escape(key)}(?![\w+#])", text, re.IGNORECASE)
        if m:
            found.append((m.start(), canon))
    return [canon for _, canon in sorted(found)]


def _tool_names(part: str) -> list[str]:
    """Часть навыка → имя технологии; из фразы — технологии каталога в ней; практика — ничего."""
    # Точка слева — часть имени (.NET), справа — конец фразы («Kafka.»).
    part = _LEADING_CONJUNCTION_RE.sub("", part.strip(" :;,-—–\"'«»").rstrip("."))
    if not part:
        return []
    known = _known_technology(part)
    if known:
        return [known]
    version = _VERSION_TAIL_RE.search(part)
    if version:
        known = _known_technology(part[: version.start()])
        if known:
            return [known]
    if _is_practice(part):
        return []
    # Русская фраза с латиницей («Тестировании бэкенда на C++») или слишком длинное имя —
    # не название технологии, но технологии каталога в ней есть.
    words = len(part.split())
    if (_CYRILLIC_RE.search(part) and words > 1) or words > _MAX_TECH_WORDS or len(part) > _MAX_TECH_CHARS:
        return _catalog_mentions(part)
    return [part]


def clean_technology_names(names: list[str]) -> list[str]:
    """Технологии из открытого словаря без практик, требований-фраз и склеенных списков.

    «Python 3.13+, FastAPI» → Python, FastAPI; «Опыт работы с мониторингом (Grafana, Kibana)» →
    Grafana, Kibana; «Функциональное тестирование», «Английский язык» → ничего. Результат —
    в каноническом написании (``canonical_technologies``).
    """
    out: list[str] = []
    for raw in names:
        name = " ".join(str(raw).split())
        if not name:
            continue
        known = _known_technology(name)
        if known:
            out.append(known)
            continue
        label = None if _SENTENCE_RE.match(name) else _LABEL_RE.match(name)
        if label and len(name) > label.end():
            name = name[label.end() :]
            known = _known_technology(name)
            if known:
                out.append(known)
                continue
        parts = _LIST_SPLIT_RE.split(name)
        if _SENTENCE_RE.match(name) or ":" in name or (len(parts) == 1 and len(name) > _MAX_TECH_CHARS):
            # Из фразы — только перечисленное в скобках и технологии каталога.
            for group in _PARENS_RE.findall(name):
                for p in _LIST_SPLIT_RE.split(group):
                    out.extend(_tool_names(p))
            out.extend(_catalog_mentions(name))
            continue
        for p in parts:
            out.extend(_tool_names(p))
    return canonical_technologies(out)


def _technologies_in_spec(name: str) -> tuple[list[str], bool]:
    """Технологии в имени специализации и признак «роль разработчика» (Python Developer)."""
    if "/" in name:
        # «C#/.NET» — технологии, только если технологии все части.
        parts = [technology_name(p) for p in name.split("/")]
        if all(parts):
            return [p for p in parts if p], False
    tech = technology_name(name)
    if tech:
        return [tech], False
    m = _TECH_ROLE_RE.match(name.strip())
    if m:
        tech = technology_name(m.group("tech") or m.group("tech2") or "")
        if tech:
            return [tech], True
    return [], False


def split_specializations(
    specializations: list[str],
    technologies: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Разделить роли и технологии: языки и фреймворки из специализаций уходят в технологии.

    ``["Automation QA", "Python"]`` → роли ``["Automation QA"]``, технологии ``[…, "Python"]``.
    «Python Developer» без другой роли даёт ``GENERIC_DEVELOPER_ROLE``; голое «Python» —
    без роли. Возвращает канонические имена обеих групп.
    """
    roles: list[str] = []
    moved: list[str] = []
    developer = False
    for raw in specializations:
        name = str(raw).strip()
        if not name:
            continue
        techs, is_dev = _technologies_in_spec(name)
        if techs:
            moved.extend(techs)
            developer = developer or is_dev
        else:
            roles.append(name)
    roles = canonical_specializations(roles)
    if developer and not roles:
        roles = [GENERIC_DEVELOPER_ROLE]
    return roles, canonical_technologies([*(technologies or []), *moved])


WORK_FORMAT_SYNONYMS: dict[str, list[str]] = {
    "remote": [
        "remote",
        "удаленка",
        "удалёнка",
        "full remote",
        "полностью удаленно",
        "полностью удалённо",
        "wfh",
        "work from home",
    ],
    "hybrid": ["hybrid", "гибрид", "гибридный"],
    "office": ["office", "офис", "онсайт", "onsite"],
    "relocation": ["relocation", "релокация", "relocate", "переезд"],
}


class SourcesConfig(BaseModel):
    channels: list[str] = Field(default_factory=list)


class AiMergeConfig(BaseModel):
    prefer_ai_for: list[str] = Field(
        default_factory=lambda: [
            "detected_specializations",
            "detected_salary_min",
            "detected_salary_max",
            "salary_currency",
            "work_format",
            "detected_grade",
            "country",
            "city",
            "application_urls",
        ]
    )
    min_confidence: float = Field(default=0.45, ge=0, le=1)


class AiEmbeddingsConfig(BaseModel):
    """Эмбеддинги вакансий и резюме (OpenRouter /embeddings): похожие, подбор, смысловой поиск."""

    enabled: bool = Field(
        default=True,
        description="Фоновая индексация вакансий и смысловые функции. Без OPENROUTER_API_KEY — пропуск.",
    )
    model: str = Field(
        default="qwen/qwen3-embedding-8b",
        description=(
            "Модель эмбеддингов (размерность 1536 — колонка vector(1536)). Смена модели — "
            "переиндексация каталога фоновой задачей; сравнить модели — evals.embeddings."
        ),
    )
    providers: list[str] = Field(
        default_factory=lambda: ["DeepInfra", "Nebius"],
        description=(
            "Порядок провайдеров OpenRouter для модели (provider.order, остальные — запасные); пусто — "
            "выбирает OpenRouter. Qwen3 у Nebius упирается в квоту токенов (429), поэтому первым DeepInfra."
        ),
    )
    query_instruction: str = Field(
        default="Given a job seeker's search query, retrieve job postings that match it",
        description=(
            "Инструкция к тексту смыслового запроса (формат Qwen3: «Instruct: …\\nQuery:…»); "
            "пусто — текст как есть (OpenAI, Gemini и др. без инструкций)."
        ),
    )
    resume_instruction: str = Field(
        default="Given a candidate's resume, retrieve job postings that suit this candidate",
        description="Инструкция к тексту резюме (подбор, карта, рынок под резюме); пусто — текст как есть.",
    )
    near_duplicate_distance: float = Field(
        default=0.013,
        ge=0,
        le=0.5,
        description=(
            "«Похожие»: ближе этого косинусного расстояния — тот же текст, не показываем. Зависит "
            "от модели (0.02 для text-embedding-3-small); пересчёт — scale.dup_threshold_equiv в eval."
        ),
    )
    sweep_interval_seconds: int = Field(
        default=300, ge=30, le=86400, description="Как часто фоновое обслуживание индексирует новые вакансии."
    )
    batch_size: int = Field(default=64, ge=1, le=256, description="Текстов в одном запросе к API.")
    max_per_sweep: int = Field(
        default=2000, ge=0, le=100_000, description="Вакансий за один проход индексации; 0 — не индексировать."
    )
    max_input_chars: int = Field(default=8000, ge=500, le=30_000, description="Символов текста на эмбеддинг.")
    map_interval_hours: int = Field(
        default=24,
        ge=0,
        le=720,
        description="Как часто фоновое обслуживание пересчитывает карту рынка; 0 — только вручную.",
    )
    map_max_points: int = Field(
        default=8000, ge=100, le=30_000, description="Вакансий на карте рынка (самые свежие за год)."
    )


class AiConfig(BaseModel):
    enabled: bool = False
    mode: str = Field(
        default="assist",
        description="assist — AI дополняет/уточняет поля; primary — AI основной источник.",
    )
    model: str = Field(
        default="",
        description="Id основной модели (из llm_models.yaml). Пусто — default_model из реестра.",
    )
    llm_models_path: str | None = Field(
        default=None,
        description="Путь к llm_models.yaml; по умолчанию — рядом с config.yaml или корень проекта.",
    )
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Запасные модели (id из llm_models.yaml). Пусто — default_fallbacks из реестра.",
    )
    match_model: str = Field(
        default="",
        description=("Модель для матчинга резюме к вакансиям (обычно reasoning). Пусто — берётся ``model``."),
    )
    match_reasoning_enabled: bool = Field(
        default=False,
        description="true для reasoning-модели матчинга (OpenRouter reasoning вместо json_object).",
    )
    match_reasoning_effort: str = Field(
        default="",
        description=(
            "Уровень рассуждений матчинга и писем (OpenRouter ``reasoning.effort``): "
            "max / xhigh / high / medium / low / minimal / none. Действует только при "
            "``match_reasoning_enabled: true``; пусто — дефолт модели. Неподдерживаемый "
            "моделью уровень OpenRouter сводит к ближайшему."
        ),
    )
    cover_letter_model: str = Field(
        default="",
        description=("Модель для генерации сопроводительных писем. Пусто — берётся ``match_model``, затем ``model``."),
    )
    base_url: str = Field(default="https://openrouter.ai/api/v1")
    max_input_chars: int = Field(default=6000, ge=500, le=32000)
    max_posts_per_run: int = Field(
        default=0,
        ge=0,
        description="0 — без лимита LLM-вызовов за прогон.",
    )
    skip_if_vacancy_score_below: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Не вызывать LLM для постов с score (0–100) ниже (0 = все посты).",
    )
    concurrency: int = Field(
        default=1,
        ge=1,
        le=16,
        description="Параллельные LLM-запросы к OpenRouter (парсинг).",
    )
    interactive_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description="Параллельные фоновые джобы мэтчинга и сопроводительных писем.",
    )
    interactive_per_user_limit: int = Field(
        default=2,
        ge=1,
        le=16,
        description="Сколько LLM-джоб (queued+running) одновременно на одного пользователя.",
    )
    requests_per_minute: float = Field(
        default=12.0,
        gt=0,
        le=500,
        description="Целевой темп запросов (пауза между вызовами к API).",
    )
    interactive_daily_request_budget: int = Field(
        default=0,
        ge=0,
        description=(
            "Суточный (UTC) бюджет запросов к провайдеру для пользовательского LLM — мэтчинг и письма "
            "всех пользователей вместе; общий счётчик в Redis, без Redis — на процесс. Исчерпан — "
            "мэтчинг и письма без LLM до полуночи UTC. Парсинг (только админ) бюджетом не ограничен. "
            "0 — без бюджета."
        ),
    )
    max_retries_on_429: int = Field(default=0, ge=0, le=10)
    retry_base_delay_seconds: float = Field(default=5.0, ge=1, le=120)
    max_retries_on_error: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Повторы при транзиентном сбое (5xx, таймаут, обрыв соединения) с backoff 2/4/8… с.",
    )
    stop_llm_when_rate_limited: bool = Field(
        default=True,
        description="При X-RateLimit-Remaining=0 / дневном лимите — отключить LLM до конца прогона (rules).",
    )
    circuit_breaker_errors: int = Field(
        default=5,
        ge=0,
        le=100,
        description=(
            "Circuit breaker прогона: столько постов подряд с ошибкой LLM (после повторов и fallback) — "
            "LLM отключается до конца прогона, как при исчерпанной квоте. 0 — выключен."
        ),
    )
    max_error_ratio: float = Field(
        default=0.1,
        ge=0,
        le=1,
        description=(
            "Режим primary: доля LLM-запросов с ошибкой, выше которой прогон считается неполным "
            "(статус partial) — посты с ошибкой отброшены. 1 — не проверять."
        ),
    )
    use_few_shot: bool = False
    prompt_version: str = Field(
        default="extract-v12",
        description=(
            "Должен совпадать с EXTRACT_PROMPT_VERSION в ai/prompts.py. "
            "Кэш всегда ключуется константой; расхождение → warning."
        ),
    )
    use_cache: bool = True
    cache_backend: str = Field(
        default="postgres",
        description="postgres — таблица ai_extraction_cache; file — локальные JSON.",
    )
    cache_ttl_days: int = Field(
        default=45,
        ge=0,
        description=(
            "Срок жизни записи кэша в днях (по умолчанию чуть больше retention.vacancy_ttl_days); "
            "0 — без срока. Записи от прежних версий промпта удаляются фоновой очисткой независимо от срока."
        ),
    )
    cache_path: str = ".cache/ai_extractions"
    request_timeout_seconds: float = 90.0
    max_completion_tokens: int = Field(
        default=5000,
        ge=64,
        le=32000,
        description="Лимит выходных токенов на один запрос (OpenRouter max_tokens).",
    )
    temperature: float = Field(default=0.1, ge=0, le=2)
    reasoning_enabled: bool = Field(
        default=False,
        description="Reasoning-модели OpenRouter: для JSON-экстракции лучше false.",
    )
    allow_unknown_specializations: bool = True
    merge: AiMergeConfig = Field(default_factory=AiMergeConfig)
    embeddings: AiEmbeddingsConfig = Field(default_factory=AiEmbeddingsConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_fallback_model(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if "fallback_models" not in data:
            legacy = data.get("fallback_model")
            if legacy:
                data = dict(data)
                data["fallback_models"] = [legacy] if isinstance(legacy, str) else legacy
        if "models" in data:
            data = dict(data)
            data.pop("models", None)
        return data

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"assist", "primary"}
        if v not in allowed:
            raise ValueError(f"ai.mode must be one of {allowed}")
        return v

    @field_validator("match_reasoning_effort", mode="before")
    @classmethod
    def validate_reasoning_effort(cls, v: object) -> str:
        normalized = str(v or "").strip().lower()
        if normalized and normalized not in REASONING_EFFORTS:
            raise ValueError(f"ai.match_reasoning_effort must be one of {REASONING_EFFORTS}")
        return normalized

    @field_validator("cache_backend")
    @classmethod
    def validate_cache_backend(cls, v: str) -> str:
        normalized = (v or "postgres").strip().lower()
        if normalized not in {"postgres", "file"}:
            raise ValueError("ai.cache_backend must be 'postgres' or 'file'")
        return normalized

    def resolved_match_model(self) -> str:
        """Модель матчинга: ``match_model`` или, если не задана, основная ``model``."""
        return self.match_model.strip() or self.model.strip()

    def resolved_cover_letter_model(self) -> str:
        """Модель писем: ``cover_letter_model`` → ``match_model`` → ``model``."""
        return self.cover_letter_model.strip() or self.resolved_match_model()

    def resolved_fallback_models(self) -> list[str]:
        """Запасные модели без дубликатов и без совпадения с ``model``."""
        primary = self.model.strip()
        seen = {primary}
        out: list[str] = []
        for raw in self.fallback_models:
            m = raw.strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def resolved_models(self) -> list[str]:
        """Все модели конфига без дубликатов: основная, fallback, матчинг, письма."""
        candidates = [
            self.model.strip(),
            *self.resolved_fallback_models(),
            self.resolved_match_model(),
            self.resolved_cover_letter_model(),
        ]
        return list(dict.fromkeys(m for m in candidates if m))


class SearchConfig(BaseModel):
    date_from: str | None = Field(
        default="week_ago",
        description=(
            "Нижняя граница: ISO-дата, либо week_ago/week/today-7d/неделя "
            "(сегодня минус 7 дней), либо пусто — без нижней границы."
        ),
    )
    date_to: str | None = Field(
        default=None,
        description=("Верхняя граница: ISO-дата, либо today/сегодня, либо пусто — при запуске подставляется сегодня."),
    )
    max_days: int = 365


class FiltersConfig(BaseModel):
    specializations: list[str] = Field(default_factory=list)
    any_specialization: bool = Field(
        default=False,
        description=(
            "Любая IT-роль: не фильтровать посты по списку specializations; "
            "LLM не отклоняет вакансию только из-за стека вне target_specializations."
        ),
    )
    exclude_keywords: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    """Пустые списки country/city означают «без фильтра по локации»."""
    work_formats: list[str] = Field(
        default_factory=list,
        description="Значения: remote, hybrid, office, relocation, any, exclude_office",
    )

    technologies: list[str] = Field(
        default_factory=list,
        description=(
            "Технологии стека (Python, Go…): вакансия проходит, если в technologies есть любая из них. "
            "Вместе со specializations — через И. Пусто — без фильтра."
        ),
    )

    @model_validator(mode="after")
    def split_roles_and_technologies(self) -> FiltersConfig:
        # Старые имена (AQA, Manual QA…) и языки в specializations (Python, Go — до миграции 037)
        # из config.yaml и снимков прогонов: языки переезжают в technologies.
        roles, techs = split_specializations(self.specializations, self.technologies)
        self.specializations = roles
        self.technologies = techs
        return self


class ParserConfig(BaseModel):
    vacancy_score_threshold: int = Field(
        default=25,
        ge=0,
        le=100,
        description="Минимальный vacancy_score правил, шкала 0–100 (один признак — 8–25 баллов).",
    )
    vacancy_min_substantive_score: int = Field(
        default=0,
        ge=0,
        le=100,
        description=(
            "Минимальный vacancy_substantive_score (0–100): баллы без учёта email/@. "
            "Например 8 отсечёт пост «только упомянули @канал без зарплаты/офис/vacancy…» при низком пороге общего счёта."
        ),
    )
    max_posts_per_channel: int = Field(
        default=5000,
        ge=1,
        description=(
            "Telethon: при заданных date_from/date_to — максимум сообщений, которые можно **просмотреть** "
            "(идём от новых к старым, пока не выйдем за date_from или не упрёмся в этот лимит)."
        ),
    )
    progress_batch_posts: int = Field(
        default=25,
        ge=1,
        le=500,
        description="Как часто отдавать снимок прогресса в UI (каждые N постов).",
    )
    db_flush_every_posts: int = Field(
        default=500,
        ge=1,
        le=100000,
        description=(
            "Промежуточная запись в БД: сливать накопленные уникальные вакансии каждые "
            "N обработанных постов или при накоплении db_flush_pending_vacancies штук — "
            "что наступит раньше (если прогон идёт через backend с vacancy_batch_sink). "
            "При падении прогона уже записанные батчи не теряются."
        ),
    )
    db_flush_pending_vacancies: int = Field(
        default=100,
        ge=1,
        le=100000,
        description=(
            "Промежуточная запись в БД: сливать, когда накопилось N ещё не записанных "
            "уникальных вакансий (или каждые db_flush_every_posts постов — что раньше)."
        ),
    )
    channel_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description=(
            "Сколько каналов опрашивать одновременно в рамках одной Telethon-сессии. "
            "1 = последовательно (старое поведение). Разумный диапазон — 3..8: больше повышает риск FloodWait."
        ),
    )
    incremental_fetch: bool = Field(
        default=True,
        description=(
            "Telethon: читать из канала только сообщения новее чекпоинта прошлого успешного прогона "
            "(если окно дат до «сегодня» и чекпоинт покрывает date_from). Чекпоинты хранит backend; "
            "false — каждый прогон перечитывает всё окно."
        ),
    )
    flood_wait_max_seconds: int = Field(
        default=300,
        ge=0,
        le=3600,
        description=(
            "Telethon: FloodWait не дольше N секунд пережидаем и продолжаем обход канала с места остановки; "
            "дольше — канал возвращается частично (с предупреждением). Короткие (до 60 с) Telethon ждёт сам."
        ),
    )
    sleep_between_requests_seconds: float = Field(
        default=1.0,
        ge=0,
        le=60,
        description=(
            "Telethon: страницы истории канала (100 сообщений) запрашиваются не чаще раза в N секунд "
            "(1 — как встроенный wait_time Telethon). 0 — без пауз. Интервал — внутри канала; "
            "каналы читаются параллельно (channel_concurrency)."
        ),
    )
    vacancy_fuzzy_duplicate_threshold: int = 88
    fuzzy_compare_prefix_len: int = 500
    fuzzy_dedup_block_by_specializations: bool = Field(
        default=True,
        description=(
            "Fuzzy-дедуп сравнивает вакансию только с группами с тем же набором специализаций, той же "
            "компанией или тем же контактом. Убирает квадрат по всем группам прогона; дубли с разными "
            "специализациями без общей компании и контакта fuzzy не ловит (точные по хэшу текста — "
            "ловит всегда). false — сравнение со всеми группами."
        ),
    )
    fuzzy_text_duplicate_threshold: int = Field(
        default=95,
        ge=0,
        le=101,
        description=(
            "Перепост с правками: сходство полного нормализованного текста (RapidFuzz ratio, 0–100) "
            "с вакансией прогона или прошлых прогонов при общей компании, контакте или наборе "
            "специализаций и совместимом грейде. 101 — выключено."
        ),
    )
    cross_run_dedup_days: int = Field(
        default=40,
        ge=0,
        le=365,
        description=(
            "За сколько дней (по дате поста) вакансии из БД участвуют в дедупе прогона: перепост с "
            "правками становится их появлением, а не новой вакансией. 0 — только внутри прогона."
        ),
    )


class FxConfig(BaseModel):
    """Курсы валют для зарплат в рублях (ЦБ РФ)."""

    enabled: bool = True
    provider: str = Field(default="cbr", description="Источник курсов (пока только cbr).")
    default_currency_if_missing: str = Field(
        default="RUB",
        description="Если salary_currency пуст — считать эту валюту.",
    )
    display_currency: str = Field(default="RUB")
    sync_days_on_startup: int = Field(
        default=7,
        ge=0,
        le=90,
        description="Сколько последних дней подтянуть в exchange_rates при старте API (0 — не грузить).",
    )
    request_timeout_seconds: float = 20.0


class RetentionConfig(BaseModel):
    """Хранение: TTL вакансий (soft delete, archived_at) и очистка служебных таблиц."""

    vacancy_ttl_days: int = Field(
        default=40,
        ge=0,
        description=(
            "Через сколько дней архивировать вакансию (по post_date, а без неё — по parsed_at). 0 — не архивировать."
        ),
    )
    parse_runs_ttl_days: int = Field(
        default=180,
        ge=0,
        description=(
            "Через сколько дней после завершения удалять прогон из parse_runs (снимок конфига, метрики) "
            "и HH sync из hh_sync_jobs. Вакансии остаются. 0 — хранить всегда."
        ),
    )
    llm_jobs_ttl_days: int = Field(
        default=30,
        ge=0,
        description=(
            "Через сколько дней удалять завершённые LLM-джобы (done/error). Матчи и письма хранятся "
            "отдельно и не удаляются. 0 — хранить всегда."
        ),
    )
    exchange_rates_ttl_days: int = Field(
        default=60,
        ge=0,
        description=(
            "Через сколько дней удалять курсы валют из exchange_rates (по rate_date). Пересчёт в ₽ "
            "берёт только последнюю дату — она остаётся всегда; окно fx.sync_days_on_startup "
            "тоже не удаляется. 0 — хранить всегда."
        ),
    )


DEFAULT_HH_SEARCH_TEXT = (
    'Python AND (автотест* OR AQA OR "QA Automation" OR pytest OR QAA OR QA Auto '
    'OR "Fullstack QA" OR Automation QA OR SDET OR автоматизатор)'
)


class HhSearchConfig(BaseModel):
    """Параметры GET /vacancies для HH sync."""

    text: str = Field(default=DEFAULT_HH_SEARCH_TEXT)
    professional_role: list[str] = Field(default_factory=lambda: ["124", "96"])
    per_page: int = Field(default=100, ge=1, le=100)


class HhConfig(BaseModel):
    """Парсинг вакансий с HH.ru (admin-only sync, без LLM)."""

    enabled: bool = True
    listing_ttl_days: int = Field(
        default=40,
        ge=0,
        description=(
            "TTL кэша короткой выдачи hh_listings (по last_seen_at). "
            "0 — не чистить. Протухшая строка → detail снова запросится."
        ),
    )
    search: HhSearchConfig = Field(default_factory=HhSearchConfig)
    max_rps: float = Field(default=3.0, gt=0, description="Лимит запросов к HH API.")
    db_flush_batch_size: int = Field(
        default=30,
        ge=1,
        le=500,
        description=(
            "Сколько Vacancy копить в памяти перед UPSERT+commit. "
            "Меньше — меньше RAM и потеря при обрыве; больше — меньше round-trip к БД."
        ),
    )
    api_base: str = Field(default="https://api.hh.ru")
    host: str = Field(default="hh.ru")
    app_name: str = Field(default="PersonalJobSearch")
    app_version: str = Field(default="1.0")


class DigestConfig(BaseModel):
    """Еженедельный обзор рынка: факты по вакансиям недели + короткий текст LLM."""

    enabled: bool = Field(
        default=True,
        description="Лидер API формирует обзор за прошедшую неделю (пн–вс, UTC), если в ней есть вакансии.",
    )
    llm_summary: bool = Field(
        default=True,
        description="Текст обзора от LLM (модель писем) по посчитанным фактам; без ключа OpenRouter — только факты.",
    )


class TelegramConfig(BaseModel):
    """Получение сообщений только через Telethon (MTProto, файл сессии)."""

    model_config = ConfigDict(extra="ignore")

    telethon_session_file: str = Field(
        default="sessions/tg_jobs.session",
        description="Файл сессии Telethon (после первой авторизации).",
    )


class DictionariesConfig(BaseModel):
    specializations: dict[str, list[str]] = Field(default_factory=dict)
    exclude_keywords: list[str] | None = None
    technologies: list[str] = Field(default_factory=list)
    work_format_synonyms: dict[str, list[str]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Корневая модель конфигурации приложения."""

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    llm_model_catalog: list[LlmModelEntry] = Field(
        default_factory=list,
        description="Каталог из llm_models.yaml (заполняется при load_config).",
    )
    search: SearchConfig = Field(default_factory=SearchConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    fx: FxConfig = Field(default_factory=FxConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    hh: HhConfig = Field(default_factory=HhConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    dictionaries: DictionariesConfig = Field(default_factory=DictionariesConfig)

    @field_validator("search")
    @classmethod
    def cap_max_days(cls, v: SearchConfig) -> SearchConfig:
        if v.max_days > 365:
            v.max_days = 365
        return v

    def merged_specializations(self) -> dict[str, list[str]]:
        merged = dict(DEFAULT_SPECIALIZATIONS)
        for raw_name, synonyms in self.dictionaries.specializations.items():
            name = SPECIALIZATION_ALIASES.get(raw_name.strip(), raw_name.strip())
            if technology_name(name):
                # Язык — не специализация: его ищет словарь технологий.
                continue
            merged.setdefault(name, [])
            merged[name] = list(dict.fromkeys(merged.get(name, []) + list(synonyms)))
        return merged

    def merged_exclude_keywords(self) -> list[str]:
        if self.dictionaries.exclude_keywords is not None:
            base = list(self.dictionaries.exclude_keywords)
        else:
            base = list(DEFAULT_EXCLUDE_KEYWORDS)
        extras = list(self.filters.exclude_keywords)
        return list(dict.fromkeys(base + extras))

    def merged_technologies(self) -> list[str]:
        tech = list(self.dictionaries.technologies) if self.dictionaries.technologies else []
        base = DEFAULT_TECHNOLOGIES + tech
        return list(dict.fromkeys(base))

    def merged_work_format_synonyms(self) -> dict[str, list[str]]:
        out = {k: list(v) for k, v in WORK_FORMAT_SYNONYMS.items()}
        for fmt, syns in self.dictionaries.work_format_synonyms.items():
            out.setdefault(fmt, [])
            out[fmt] = list(dict.fromkeys(out.get(fmt, []) + list(syns)))
        return out

    def to_serializable_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")
