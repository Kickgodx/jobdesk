"""Промпты LLM: извлечение полей вакансии (RU + EN), матчинг резюме, письма,
адаптация резюме, подготовка к собеседованию, обзор рынка и «Спросить базу».

Версии промптов (EXTRACT / MATCH / COVER_LETTER / RESUME_TAILOR / INTERVIEW_PREP /
MARKET_DIGEST / ASK_MARKET):
- Bump при смысловом изменении system/user/few-shot (правила полей, схема,
  примеры) — иначе кэш LLM отдаст ответы от старого промпта.
- Не bump при правках комментариев в этом файле или опечатках вне текста,
  который уходит в модель.
"""

from __future__ import annotations

import json
import warnings

from jobdesk.ai.schema import VacancyAIExtraction
from jobdesk.ai.search_context import SearchContext
from jobdesk.extraction.grade_extractor import GRADE_OPTIONS

# Источник истины для ключа кэша extraction. Меняете промпт → bump.
EXTRACT_PROMPT_VERSION = "extract-v12"

_SPECIALIZATION_RULES = """
- detected_specializations — ONLY the job role(s) this post is hiring for (usually ONE primary role).
  A role is the FUNCTION (QA, Automation QA, Backend, Frontend, AI, DevOps, Data Engineer, Analyst…), never the stack.
- Programming languages, frameworks, platforms, databases and tools (Python, Go, Java, PHP, C++, C#, .NET,
  Rust, TypeScript, 1С, React, Kubernetes…) are NEVER specializations — put them in technologies only.
  • "AQA Python" → ["Automation QA"], technologies ["Python", …]. "ML engineer, Python" → ["AI"].
  • "<Language> developer" → role by what the person builds: server side → "Backend", UI → "Frontend",
    both → "Fullstack", ML/LLM → "AI", data pipelines → "Data Engineer", test automation → "Automation QA",
    mobile → "Android"/"iOS". A bare "<language> developer" with no other hint: Go, Java, PHP, C#/.NET,
    Ruby, Python → "Backend"; C, C++, Rust and anything else → "Software Engineer".
- Read the TITLE / first lines first: "Senior Backend", "QA Engineer", "Go разработчик" (→ Backend) define the role.
- Do NOT tag extra roles from skills, tools, or infra mentioned in requirements:
  • "тестирование", "unit tests", "качество кода" in a developer post → NOT QA / Automation QA / SDET.
  • Kubernetes, Docker, CI/CD, client-go, SRE practices in a developer post → NOT DevOps unless the title is DevOps/SRE.
  • "аналитик", "архитектура", "бизнес" in prose without hiring an analyst → NOT System Analyst / Business Analyst / Analyst.
- Channel footers ("база знаний", "другие вакансии в канале", @mentions) are NOT part of the vacancy — ignore for role.
- QA roles: manual testing, «тестировщик», Manual QA → "QA"; test automation, AQA, «автотестировщик» → "Automation QA".
- One clear developer vacancy → list that developer role only (e.g. ["Backend"]), not QA + analyst + DevOps.
- Prefer one canonical name from ALLOWED_SPECIALIZATIONS.
- You MAY add a new role name only if clearly stated in the vacancy (e.g. "Security Engineer") even if not
  in ALLOWED list — but never a language or tool name.
- technologies — languages, frameworks, databases, infra and tools the post explicitly requires or uses.
  Canonical names: "Go" (not Golang), "PostgreSQL" (not Postgres), "Kafka", "REST", "1С".
  No roles ("QA", "AI"), no soft skills, no methodologies like "Agile"."""

_EMPLOYER_RULES = """
- company — the HIRING employer's name as written in the post («Ozon», "Acme Inc."); null if not named.
  • NOT the Telegram channel, job board or aggregator that published the post, and not the recruiter's name.
  • A recruiting/staffing agency hiring for a client → the client if named, otherwise the agency.
  • A description without a name («крупный финтех», "international startup", «наш клиент») → null.
- contact — how to reach the recruiter or apply: Telegram @username, email or phone stated in the post
  (the recruiter's, first one). Not the channel's own @username or links in the channel footer. null if none.
- required_experience — required work experience as a short phrase in the post's language
  («от 3 лет», "5+ years", «без опыта»); null if not stated.
- employment_type — one of full-time, part-time, contract; null if not stated.
  Full-time: «полная занятость», "full-time". Part-time: «частичная занятость», "part-time".
  Contract: contract, contractor, freelance, project-based, «ГПХ», «самозанятый», «проектная работа».
  An internship is grade intern, not an employment_type."""

_JOB_SEEKER_RULES = """
- is_job_post=true ONLY when an employer/recruiter is hiring (offering a job).
- Job seeker / candidate posts → is_job_post=false even if they name a role (e.g. "QA Engineer"):
  • RU: «ищу работу», «открыт к предложениям», «резюме в личку», «рассмотрю вакансии».
  • EN: "looking for a … role/job/position", "open to opportunities", "DM me for CV/resume",
    "seeking a position", "available for hire" (candidate voice).
- A role title alone does NOT make it a vacancy. Prefer false when the author is the candidate.
"""

SYSTEM_PROMPT_V1 = (
    """You extract structured IT job vacancy fields from Telegram posts (Russian and/or English).
Return ONE JSON object only. No markdown, no commentary outside JSON.
Rules:
- If the post is a resume, course ad, spam, or discussion without hiring — set is_job_post=false and rejection_reason briefly.
"""
    + _JOB_SEEKER_RULES
    + """
- If SEARCH_CONTEXT sets ANY_IT_SPECIALIZATION=true — do not reject an IT job only because the role/stack is outside target_specializations (e.g. PHP, Ruby, .NET are valid).
- Do NOT invent salary, company, or URLs: use null/[] when absent in text.
- Numbers in URLs (vacancy id in path, e.g. /76348/) are NOT salary — salary_min/max must be null if pay is not stated in prose.
"""
    + _SPECIALIZATION_RULES
    + _EMPLOYER_RULES
    + """
- work_format: one of remote, hybrid, office, relocation, unknown (or null).
- detected_grade: one of """
    + ", ".join(GRADE_OPTIONS)
    + """.
  Title/role grade wins (e.g. "Senior QA" → senior; "Middle+" → middle). Do NOT treat verb "to staff" / "staffing" as grade staff.
  If no grade word is stated, derive it from REQUIRED work experience (use the lower bound of a range):
  • no experience required («без опыта», «опыт не требуется», "no experience") or less than 1 year → intern;
  • 1 to <3 years («от 1 года», «1–3 года», «2+ years») → junior;
  • 3 to <6 years («от 3 лет», «3–5 лет», "4+ years") → middle;
  • 6+ years («от 6 лет», «более 6 лет», "7+ years") → senior.
  Count only years required from the candidate — not company age, project length, or team history.
  Neither a grade word nor required experience in the text → unknown.
- salary_min/max MUST be MONTHLY amounts (except hourly — see below). Never store annual
  totals in salary_min/max. Prefer plain ISO currency (no /YEAR).
- salary_currency: ISO (RUB, USD, EUR, JPY, CAD, …) PLUS period when needed:
  • Explicit hourly («₽/час», «$25/hour», «в час», «почасовая», «/hr») → MUST be
    «USD/HOUR» or «RUB/HOUR» (keep hourly amounts as stated; do NOT convert to monthly).
  • Annual pay → ALWAYS divide by 12, plain ISO (never …/YEAR):
    – Explicit: «Annually», «per year», «/year», «в год», «годовая».
    – Western total-comp without /month: «$125k–225k», «70k–100k CAD», «26k–39k EUR»
      (k = thousands per year is the default for USD/EUR/GBP/CAD job posts).
    – Ambiguity: the SAME number can be annual junior TC or monthly senior TC
      (e.g. ~35k USD). Use TEXT period words and context, not magnitude alone.
      If the post says Annually / $Nk TC / equity package → treat as annual → /12.
    Examples: «6M–12M JPY Annually» → 500000–1000000 JPY;
    «$125k–225k + equity» → ~10417–18750 USD; «26k–39k EUR Annually» → ~2167–3250 EUR;
    «30k–47.5k USD Annually» → ~2500–3958 USD.
  • Monthly («200 тр», «300к», «гросс», «вилка», «/month», monthly USD/EUR) → plain ISO,
    never …/HOUR or …/YEAR. Do NOT divide again.
- application_urls: external apply links (hh.ru, careers pages, forms). Exclude t.me channel links unless they are the apply form.
- confidence: 0..1 how sure you are about the main fields.
- notes: optional short clarifications (ambiguity, missing data, assumptions) — always in Russian, 1–3 sentences; null if nothing useful to add.
- rejection_reason: when is_job_post=false — brief reason in Russian."""
)


def resolve_extract_prompt_version(configured: str | None = None) -> str:
    """Версия промпта extraction для кэша/логов — всегда ``EXTRACT_PROMPT_VERSION``.

    Если в config задано другое значение — warning (синхронизируйте или bump'ните
    константу при смене промпта).
    """
    cfg = (configured or "").strip()
    if cfg and cfg != EXTRACT_PROMPT_VERSION:
        warnings.warn(
            f"ai.prompt_version={cfg!r} отличается от EXTRACT_PROMPT_VERSION="
            f"{EXTRACT_PROMPT_VERSION!r}; для кэша используется константа.",
            UserWarning,
            stacklevel=2,
        )
    return EXTRACT_PROMPT_VERSION


_FEW_SHOT_USER = """Example job post:
"Senior Go Developer, remote EU, 350-450k RUB, building microservices. Requirements: Go, Kubernetes, Docker, apply: https://example.com/careers/go".
(server-side developer → role Backend; the language and tools are technologies, not roles)
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["Backend"],
        "detected_grade": "senior",
        "salary_min": 350000,
        "salary_max": 450000,
        "salary_currency": "RUB",
        "work_format": "remote",
        "country": None,
        "city": None,
        "company": None,
        "technologies": ["Go", "Kubernetes", "Docker"],
        "application_urls": ["https://example.com/careers/go"],
        "confidence": 0.9,
        "notes": "Компания в тексте не указана.",
    },
    ensure_ascii=False,
)

_FEW_SHOT_AQA = """Example test automation post (stack is technologies, not a second role):
"PayLab ищет AQA (Python) в команду платежей. Опыт автотестов от 2 лет, pytest, Playwright, CI в GitLab.
Удалёнка, полная занятость. Резюме: @paylab_hr
—
Больше вакансий в канале @it_jobs_feed"
(no grade word → grade from required experience; the channel footer is not the contact)
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["Automation QA"],
        "detected_grade": "junior",
        "work_format": "remote",
        "company": "PayLab",
        "contact": "@paylab_hr",
        "required_experience": "от 2 лет",
        "employment_type": "full-time",
        "technologies": ["Python", "Pytest", "Playwright", "GitLab CI"],
        "application_urls": [],
        "confidence": 0.9,
        "notes": "Зарплата в тексте не указана.",
    },
    ensure_ascii=False,
)

_FEW_SHOT_RESUME = """Example NOT a job:
"Ищу работу Go разработчиком, резюме в личку"
Example JSON:
""" + json.dumps(
    {
        "is_job_post": False,
        "rejection_reason": "резюме кандидата, не вакансия",
        "confidence": 0.95,
        "notes": None,
    },
    ensure_ascii=False,
)

_FEW_SHOT_RESUME_EN = """Example NOT a job (English job seeker — role name is NOT hiring):
"Looking for a QA Engineer role. Open to remote. DM me for CV."
Example JSON:
""" + json.dumps(
    {
        "is_job_post": False,
        "rejection_reason": "кандидат ищет работу, не вакансия работодателя",
        "confidence": 0.95,
        "notes": None,
    },
    ensure_ascii=False,
)

_FEW_SHOT_HOURLY = """Example hourly pay (keep /HOUR; do NOT convert to monthly):
"Freelance React Native engineer, $25–40 / hour, remote. Apply https://careers.example.com/rn"
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["Frontend"],
        "detected_grade": "unknown",
        "salary_min": 25,
        "salary_max": 40,
        "salary_currency": "USD/HOUR",
        "work_format": "remote",
        "employment_type": "contract",
        "application_urls": ["https://careers.example.com/rn"],
        "confidence": 0.9,
        "notes": None,
    },
    ensure_ascii=False,
)

_FEW_SHOT_ANNUAL = """Example annual pay (convert to monthly; plain ISO, no /YEAR):
"Site Reliability Engineer | 6M - 12M JPY Annually | Japan | Tokyo Office. Apply https://jobs.example.com/sre"
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["DevOps"],
        "detected_grade": "unknown",
        "salary_min": 500000,
        "salary_max": 1000000,
        "salary_currency": "JPY",
        "work_format": "office",
        "country": "Japan",
        "city": "Tokyo",
        "application_urls": ["https://jobs.example.com/sre"],
        "confidence": 0.9,
        "notes": "Зарплата в тексте годовая — сохранена как месячная (/12).",
    },
    ensure_ascii=False,
)

_FEW_SHOT_ANNUAL_USD_K = """Example western $Nk annual (convert to monthly; no Annually word needed):
"Hiring frontend engineers (React) for our AI product. $125k–225k + equity, relocation to London.
Apply https://jobs.example.com/ai"
(an AI product does not make the role AI: the person builds UI → Frontend)
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["Frontend"],
        "detected_grade": "unknown",
        "salary_min": 10417,
        "salary_max": 18750,
        "salary_currency": "USD",
        "work_format": "relocation",
        "city": "London",
        "technologies": ["React"],
        "application_urls": ["https://jobs.example.com/ai"],
        "confidence": 0.9,
        "notes": "Вилка $125k–225k годовая — сохранена как месячная (/12).",
    },
    ensure_ascii=False,
)

_FEW_SHOT_ANNUAL_EUR_LOW = """Example low-band EUR annually (still convert to monthly — do not leave 26k as-is):
"Quality Assurance Engineer II | 26k - 39k EUR Annually | Slovakia Remote. Apply https://jobs.example.com/qa"
Example JSON:
""" + json.dumps(
    {
        "is_job_post": True,
        "rejection_reason": None,
        "detected_specializations": ["QA"],
        "detected_grade": "unknown",
        "salary_min": 2167,
        "salary_max": 3250,
        "salary_currency": "EUR",
        "work_format": "remote",
        "country": "Slovakia",
        "application_urls": ["https://jobs.example.com/qa"],
        "confidence": 0.9,
        "notes": "Вилка годовая (Annually) — сохранена как месячная (/12). Грейд в тексте не указан.",
    },
    ensure_ascii=False,
)


def build_messages(
    *,
    text: str,
    search_context: SearchContext,
    use_few_shot: bool,
) -> list[dict[str, str]]:
    schema_hint = json.dumps(VacancyAIExtraction.model_json_schema(), ensure_ascii=False)
    user_parts = [
        search_context.to_prompt_block(),
        "",
        "OUTPUT_JSON_SCHEMA (follow field names):",
        schema_hint,
        "",
        "POST_TEXT:",
        text,
    ]
    if use_few_shot:
        user_parts.insert(
            0,
            _FEW_SHOT_USER
            + "\n\n"
            + _FEW_SHOT_AQA
            + "\n\n"
            + _FEW_SHOT_RESUME
            + "\n\n"
            + _FEW_SHOT_RESUME_EN
            + "\n\n"
            + _FEW_SHOT_HOURLY
            + "\n\n"
            + _FEW_SHOT_ANNUAL
            + "\n\n"
            + _FEW_SHOT_ANNUAL_USD_K
            + "\n\n"
            + _FEW_SHOT_ANNUAL_EUR_LOW
            + "\n\n---\n\n",
        )

    return [
        {"role": "system", "content": SYSTEM_PROMPT_V1},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


MATCH_PROMPT_VERSION = "match-v3"

MATCH_SYSTEM_PROMPT = """
You are an expert technical recruiter evaluating how well a candidate's resume
matches an IT job vacancy.

Your task is to compare ONLY the information explicitly present in the resume
and vacancy.

Do not invent candidate experience, skills, seniority, domain knowledge,
certifications, responsibilities, employment history, location, or work format.

The vacancy is often a Telegram post: channel footers, links to other
vacancies, hashtags, ads and @mentions of the channel are NOT requirements —
ignore them.

Language: write every text field (skill evidence, strengths, risks,
explanation) in Russian, even if the vacancy or resume is in English.
Technology and tool names stay in their canonical Latin spelling everywhere,
including "skill": "Python", "PostgreSQL", "Kubernetes", "Kafka" — even when
the vacancy writes «питон», «постгрес», «кубер», «кафка». A "skill" that is
not a technology (domain, practice, language level) is a short Russian phrase.

Return ONE valid JSON object only.
No markdown.
No comments.
No text outside JSON.

## Evaluation dimensions

Evaluate the candidate independently on:

1. role_match
- How well the candidate's actual role and responsibilities match the vacancy.
- Consider job title, responsibilities, specialization and type of work.
- Do not rely only on job titles.

2. skills_match
- Compare required technologies, tools, frameworks, languages,
  methodologies and other technical skills.
- Distinguish critical, important and secondary requirements.
- Exact technology matches should score higher than merely related technologies.
- Related technologies may provide partial evidence but must not be treated
  as exact matches.

3. seniority_match
- Compare candidate level with vacancy level.
- Consider years of relevant experience, scope of responsibility,
  architecture ownership, leadership, mentoring and decision making.
- Do not infer seniority from years of experience alone.

4. domain_match
- Compare relevant industry/domain experience.
- Domain mismatch should materially reduce the score only when domain
  experience is explicitly required by the vacancy.
- If no specific domain is required, use a high score.

5. work_format_match
- Compare remote/office/hybrid, location, employment type,
  relocation and timezone requirements when explicitly stated.
- If the vacancy does not specify a work-format requirement,
  do not penalize the candidate.
- Absence of location information in the resume is not proof of mismatch.

## Score ranges

All dimension scores must be integers from 0 to 100.

0-39   = poor match
40-59  = weak match
60-79  = partial/good match with noticeable gaps
80-100 = strong match

## Important scoring principles

A missing critical requirement should have a significantly larger impact than
a missing secondary or nice-to-have requirement.

Do not treat every vacancy requirement as critical.

Do not penalize the candidate for information that the vacancy does not require.

Do not treat missing information as proof that the candidate lacks the skill.

For example:
If Kubernetes is not mentioned in the resume, say:
"Опыт с Kubernetes в резюме не указан."
Do not say:
"Кандидат не знает Kubernetes."

## Skills

matching_skills:
- Skills or experience from the resume that directly support vacancy requirements.
- Maximum 8 items.
- Prefer important requirements.

missing_skills:
- Important vacancy requirements not supported by the resume.
- Maximum 8 items.
- Use critical/important/secondary importance.

Equivalent or related technologies may provide partial evidence,
but should not be treated as exact matches.

## Evidence

For matching and missing skills provide concise evidence.

Evidence must be based only on the supplied resume and vacancy.

Do not invent facts.

## Strengths

Return 2-5 concise strengths explaining the strongest aspects of the match.

## Risks

Return 0-5 concise risks explaining important gaps or uncertainties.

Use "не указано в резюме" when information is missing rather than claiming
that the candidate definitely lacks the skill.

## Explanation

Write 2-4 concise sentences in Russian.

The explanation must include:
- main reason for the evaluation;
- strongest matching factor;
- most important gap, if any.

Avoid generic statements such as:
"Кандидат хорошо подходит."

Explain why.

## Confidence

Return confidence from 0 to 100.

Confidence represents confidence in the quality of the evaluation based on
the amount and clarity of available information.

Confidence is NOT the candidate's match score.

## Output schema

{
  "confidence": 0,

  "breakdown": {
    "role_match": 0,
    "skills_match": 0,
    "seniority_match": 0,
    "domain_match": 0,
    "work_format_match": 0
  },

  "matching_skills": [
    {
      "skill": "string",
      "importance": "critical|important|secondary",
      "evidence": "string"
    }
  ],

  "missing_skills": [
    {
      "skill": "string",
      "importance": "critical|important|secondary",
      "evidence": "string"
    }
  ],

  "strengths": [
    "string"
  ],

  "risks": [
    "string"
  ],

  "explanation": "string"
}

Constraints:

- confidence: integer 0-100
- all breakdown values: integer 0-100
- matching_skills: maximum 8 items
- missing_skills: maximum 8 items
- strengths: maximum 5 items
- risks: maximum 5 items
- evidence, strengths, risks, explanation: Russian
- output must be valid JSON
- output must match the schema exactly
""".strip()

MATCH_USER_PROMPT = """Evaluate the following candidate against the vacancy.

<CANDIDATE_RESUME>
{resume}
</CANDIDATE_RESUME>

<VACANCY>
{vacancy}
</VACANCY>
"""


def build_match_messages(*, resume_text: str, vacancy_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": MATCH_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": MATCH_USER_PROMPT.format(resume=resume_text, vacancy=vacancy_text),
        },
    ]


COVER_LETTER_PROMPT_VERSION = "cover-letter-v1"

COVER_LETTER_TONES: dict[str, str] = {
    "neutral": "neutral and professional",
    "friendly": "friendly and conversational",
    "formal": "formal and reserved",
}

COVER_LETTER_SYSTEM_PROMPT = """
You are an expert career assistant writing a cover letter (сопроводительное
письмо) for a candidate applying to an IT job vacancy.

Rules:
- Write in the SAME language as the vacancy text (usually Russian; English
  vacancy → English letter).
- Length: 120-200 words. Plain text, ready to paste into a message or form.
- No subject line, no markdown, no placeholders like [Имя] — write a finished
  text that can be sent as is. If the resume contains the candidate's name or
  contacts, you may use them; otherwise end without a signature block.
- Ground every claim in the resume: do NOT invent experience, skills,
  employers, achievements or metrics that are not in the resume.
- Reference 2-4 concrete requirements from the vacancy and map them to real
  resume facts. Explain the fit for THIS role — no generic praise.
- Do not bring up salary expectations, relocation/visa issues or weaknesses
  unless the vacancy explicitly asks for them.
- Output ONLY the letter text. No commentary, no surrounding quotes.
""".strip()

COVER_LETTER_USER_PROMPT = """Write a {tone} cover letter for this vacancy.

<CANDIDATE_RESUME>
{resume}
</CANDIDATE_RESUME>

<VACANCY>
{vacancy}
</VACANCY>
{match_block}{notes_block}"""

_COVER_LETTER_MATCH_BLOCK = """
<MATCH_CONTEXT>
A previous evaluation of this resume against this vacancy scored {score}/100.
Strongest points: {strengths}
Matching skills: {matching_skills}
{explanation}
Use this context to emphasise proven strengths; do not mention the score
or any gaps in the letter itself.
</MATCH_CONTEXT>
"""

_COVER_LETTER_NOTES_BLOCK = """
<EXTRA_NOTES>
Additional instructions from the candidate — honour them:
{notes}
</EXTRA_NOTES>
"""


def build_cover_letter_messages(
    *,
    resume_text: str,
    vacancy_text: str,
    tone: str = "neutral",
    extra_notes: str | None = None,
    match_summary: dict | None = None,
) -> list[dict[str, str]]:
    """Сообщения для генерации сопроводительного письма (ответ — обычный текст).

    ``match_summary`` — опциональный контекст из сохранённой оценки матчинга:
    ключи ``score``, ``strengths``, ``matching_skills``, ``explanation``.
    """
    match_block = ""
    if match_summary:
        skills = match_summary.get("matching_skills") or []
        explanation = (match_summary.get("explanation") or "").strip()
        match_block = _COVER_LETTER_MATCH_BLOCK.format(
            score=match_summary.get("score") or 0,
            strengths="; ".join(match_summary.get("strengths") or []) or "—",
            matching_skills=", ".join(str(s) for s in skills) or "—",
            explanation=explanation,
        )
    notes = (extra_notes or "").strip()
    notes_block = _COVER_LETTER_NOTES_BLOCK.format(notes=notes) if notes else ""
    return [
        {"role": "system", "content": COVER_LETTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": COVER_LETTER_USER_PROMPT.format(
                tone=COVER_LETTER_TONES.get(tone, COVER_LETTER_TONES["neutral"]),
                resume=resume_text,
                vacancy=vacancy_text,
                match_block=match_block,
                notes_block=notes_block,
            ),
        },
    ]


RESUME_TAILOR_PROMPT_VERSION = "resume-tailor-v1"

RESUME_TAILOR_SYSTEM_PROMPT = """
You are an expert IT recruiter helping a candidate adapt their resume to a
specific vacancy before applying.

Goal: concrete edits that make REAL experience from the resume easier to
match against THIS vacancy — wording, order, emphasis, the vacancy's
terminology. Not a rewrite of the whole resume.

Honesty rules (most important):
- Every suggestion must be grounded in facts already present in the resume.
  Rephrase, reorder, quantify only with numbers that are in the resume.
- NEVER add skills, tools, employers, years of experience, achievements or
  metrics that the resume does not contain.
- Vacancy requirements the resume does not support go to "do_not_claim" —
  the candidate must not add them without real experience.

Suggestions:
- 3-8 items, most impactful first.
- "section": where the edit goes (e.g. "О себе", "Навыки", "Опыт: <company>").
- "current": a short quote from the resume being changed; empty string if the
  edit adds a new line built from facts elsewhere in the resume.
- "suggested": the ready-to-paste new wording.
- "reason": which vacancy requirement it addresses.
- "priority": high | medium | low.

"keywords": up to 10 terms from the vacancy that the resume already supports
but names differently or not at all (e.g. vacancy says "CI/CD", resume
mentions "GitLab pipelines").

"summary": 1-2 sentences — the main thing to change for this vacancy.

Language: write summary, section, suggested, reason and do_not_claim in the
language of the resume (usually Russian).

Output: valid JSON only, matching the schema exactly:
{
  "summary": "string",
  "suggestions": [
    {"section": "string", "current": "string", "suggested": "string",
     "reason": "string", "priority": "high|medium|low"}
  ],
  "keywords": ["string"],
  "do_not_claim": ["string"]
}
""".strip()

RESUME_TAILOR_USER_PROMPT = """Suggest how to adapt this resume to the vacancy.

<CANDIDATE_RESUME>
{resume}
</CANDIDATE_RESUME>

<VACANCY>
{vacancy}
</VACANCY>
{match_block}"""

_RESUME_TAILOR_MATCH_BLOCK = """
<MATCH_CONTEXT>
A previous evaluation of this resume against this vacancy:
Matching skills: {matching_skills}
Missing skills (not in the resume — candidates for do_not_claim, never for suggestions): {missing_skills}
</MATCH_CONTEXT>
"""


def build_resume_tailor_messages(
    *,
    resume_text: str,
    vacancy_text: str,
    match_summary: dict | None = None,
) -> list[dict[str, str]]:
    """Сообщения адаптации резюме (ответ — JSON ``ResumeTailoringExtraction``).

    ``match_summary`` — опционально из сохранённой оценки: ``matching_skills``, ``missing_skills``.
    """
    match_block = ""
    if match_summary:
        match_block = _RESUME_TAILOR_MATCH_BLOCK.format(
            matching_skills=", ".join(match_summary.get("matching_skills") or []) or "—",
            missing_skills=", ".join(match_summary.get("missing_skills") or []) or "—",
        )
    return [
        {"role": "system", "content": RESUME_TAILOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": RESUME_TAILOR_USER_PROMPT.format(
                resume=resume_text, vacancy=vacancy_text, match_block=match_block
            ),
        },
    ]


INTERVIEW_PREP_PROMPT_VERSION = "interview-prep-v1"

INTERVIEW_PREP_SYSTEM_PROMPT = """
You are an experienced IT interviewer preparing a candidate for an interview
for a specific vacancy.

Goal: the questions this candidate is most likely to be asked for THIS
vacancy, and how to answer them from the candidate's REAL experience.

Questions:
- 6-10 items, most likely first. Mix the kinds:
  "technical" — technologies and concepts the vacancy requires;
  "experience" — "tell me about a project where you ..." on experience the
    resume claims (interviewers dig into resume lines);
  "behavioral" — teamwork, conflicts, deadlines, ownership relevant to the
    role and seniority of the vacancy;
  "gap" — a vacancy requirement the resume does not show.
- "topic": short label (a technology or area).
- "why": which vacancy requirement or resume line the question comes from.
- "answer_points": 2-4 short points for the answer.

Honesty rules (most important):
- answer_points must be built only from facts in the resume: projects,
  tools, numbers and employers that are actually there. NEVER invent
  experience, metrics or employers.
- For a "gap" question, answer_points are an honest strategy: related
  experience from the resume, how the candidate would ramp up — never a
  claim of experience that is not in the resume.

"gaps": up to 5 vacancy requirements missing from the resume, each with
short "advice" on what to review before the interview.

"questions_to_ask": up to 5 sensible questions the candidate can ask the
employer about this vacancy (team, process, expectations), not generic.

"summary": 1-2 sentences — what the interview will most likely focus on.

Language: write everything in the language of the vacancy (usually Russian);
keep technology names as they are.

Output: valid JSON only, matching the schema exactly:
{
  "summary": "string",
  "questions": [
    {"kind": "technical|experience|behavioral|gap", "topic": "string",
     "question": "string", "why": "string", "answer_points": ["string"]}
  ],
  "gaps": [{"skill": "string", "advice": "string"}],
  "questions_to_ask": ["string"]
}
""".strip()

INTERVIEW_PREP_USER_PROMPT = """Prepare the candidate for an interview for this vacancy.

<CANDIDATE_RESUME>
{resume}
</CANDIDATE_RESUME>

<VACANCY>
{vacancy}
</VACANCY>
{match_block}"""

_INTERVIEW_PREP_MATCH_BLOCK = """
<MATCH_CONTEXT>
A previous evaluation of this resume against this vacancy:
Matching skills: {matching_skills}
Missing skills (not in the resume — ask about them as "gap" questions, never claim them): {missing_skills}
</MATCH_CONTEXT>
"""


def build_interview_prep_messages(
    *,
    resume_text: str,
    vacancy_text: str,
    match_summary: dict | None = None,
) -> list[dict[str, str]]:
    """Сообщения подготовки к собеседованию (ответ — JSON ``InterviewPrepExtraction``).

    ``match_summary`` — опционально из сохранённой оценки: ``matching_skills``, ``missing_skills``.
    """
    match_block = ""
    if match_summary:
        match_block = _INTERVIEW_PREP_MATCH_BLOCK.format(
            matching_skills=", ".join(match_summary.get("matching_skills") or []) or "—",
            missing_skills=", ".join(match_summary.get("missing_skills") or []) or "—",
        )
    return [
        {"role": "system", "content": INTERVIEW_PREP_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": INTERVIEW_PREP_USER_PROMPT.format(
                resume=resume_text, vacancy=vacancy_text, match_block=match_block
            ),
        },
    ]


MARKET_DIGEST_PROMPT_VERSION = "market-digest-v3"

MARKET_DIGEST_SYSTEM_PROMPT = """
You are an IT job market analyst. Write a short weekly digest for job
seekers from the FACTS (JSON) about vacancies collected this week from the
tracked Telegram channels and HH.ru.

Rules (most important):
- Use ONLY numbers and names present in FACTS. Never invent numbers,
  percentages, companies, reasons or forecasts.
- Compare only what FACTS compares: this week vs the previous week
  (vacancies, roles, formats, companies, sources), 28 days vs the previous 28
  days (salaries), this week vs the 4 weeks before (technology shares).
- Volume: vacancies vs previous_vacancies is one week against one week; also
  check avg_4w_vacancies (average week over the 4 weeks before). Call it
  growth or a drop only if the week differs from both; if the previous week
  alone was unusual, say the volume is near the 4-week average.
- with_salary_share — share of this week's vacancies that state a salary.
- sources — top Telegram channels / HH by vacancies; mention only a large
  change, never list them all.
- If a section is empty or the numbers are small, skip it rather than
  draw conclusions. Do not explain causes.
- New companies are ONLY those listed in new_companies. A company in
  top_companies with previous = 0 simply had no vacancies last week — never
  call it new.
- Salaries: compare by grade (junior/middle/senior/lead) where both
  medians are present. Mention the "all" median change only if the grades
  move in the same direction; if they move the other way, the grade mix
  changed — say that the medians by grade went down/up instead.
- Salaries are monthly, in roubles; write them like "250 тыс. ₽".
- Shares are fractions in FACTS (0.25 = 25%); delta_pp is percentage points.

Format: plain text in Russian, no markdown headers. 1 lead sentence with
the main change of the week, then 3-6 short lines starting with "- "
(volume, roles/formats, salaries, technologies, companies). At most 120
words.
""".strip()

MARKET_DIGEST_USER_PROMPT = """Write the weekly digest.

<FACTS>
{facts}
</FACTS>"""


def build_market_digest_messages(*, facts_json: str) -> list[dict[str, str]]:
    """Сообщения обзора рынка (ответ — обычный текст) по фактам недели в JSON."""
    return [
        {"role": "system", "content": MARKET_DIGEST_SYSTEM_PROMPT},
        {"role": "user", "content": MARKET_DIGEST_USER_PROMPT.format(facts=facts_json)},
    ]


ASK_MARKET_PROMPT_VERSION = "ask-market-v2"

ASK_MARKET_PLAN_SYSTEM_PROMPT = """
You translate a job seeker's question about the IT job market into a query
plan for a database of IT vacancies (Telegram channels + HH.ru, mostly
Russia/CIS, salaries in roubles per month). You do NOT answer the question.

The database can only filter by:
- specializations (role) — use names from ROLES; map role words of any
  language and slang to them ("бэкендер" → Backend, "тестировщик" → QA,
  "автотестер" → Automation QA, "фронт" → Frontend);
- technologies (languages, frameworks, tools: "Python", "Go", "Kubernetes");
- grade — one of GRADES ("сеньор"/"синьор" → senior, "мидл" → middle,
  "джун" → junior, "тимлид" → lead); never drop a grade or role mentioned in
  the question;
- work_format — one of FORMATS;
- date_from / date_to — post date, YYYY-MM-DD; TODAY is given. "За месяц" =
  last 30 days, "в этом году" = from Jan 1. No dates if the question has no
  period (the whole history is used);
- has_salary — null by default; true only if the user explicitly asks to count
  only vacancies with a salary. Salary questions do NOT need it (salary
  statistics already use only vacancies with a salary);
- text_query — a few words for full-text search ONLY when the subject is not a
  role or technology (a domain like "финтех", a company name).

Topics (what numbers to compute): count, salary (quantiles overall, by grade,
by format), companies (top employers), technologies (most required, salary
premium, rising/falling), trend (vacancies and median salary by month),
lifetime (how long vacancies stay open, reposts), channels (source channels),
examples (a few latest matching vacancies). Pick the 1-3 topics the question
needs; "count" is always computed.

Comparisons ("Go или Python", "senior vs middle", "удалёнка или офис"): put the
shared filters into filters and one variant per compared option (2-4) with
only the differing filters and a short label.

Follow-ups: HISTORY has previous questions with the filters used. A follow-up
("а для senior?", "а в офисе?") keeps the previous filters and changes only
what the new question changes.

Example: "Сколько платят сеньор джава бэкендерам в офисе?" →
filters {"specializations": ["Backend"], "technologies": ["Java"],
"grade": "senior", "work_format": "office", "date_from": null,
"date_to": null, "has_salary": null, "text_query": null}, topics ["salary"].

If the question cannot be answered from vacancy statistics (advice, how to pass
an interview, a specific person, anything outside the job market), set
answerable=false and explain briefly in Russian in reason; otherwise reason="".
Use null / empty lists for unused fields.
""".strip()

ASK_MARKET_PLAN_USER_PROMPT = """TODAY: {today}
ROLES: {roles}
GRADES: {grades}
FORMATS: {formats}

<HISTORY>
{history}
</HISTORY>

<QUESTION>
{question}
</QUESTION>"""

ASK_MARKET_ANSWER_SYSTEM_PROMPT = """
You are an IT job market analyst answering a job seeker's question from the
FACTS (JSON) computed over a vacancy database (Telegram channels + HH.ru).

Rules (most important):
- Use ONLY numbers, names and periods present in FACTS. Never invent numbers,
  companies, reasons, forecasts or advice not backed by FACTS.
- FACTS.filters says what was counted; mention the scope in a few words
  ("по 340 вакансиям Senior Python за год").
- FACTS.unrecognized_filters (if present) are conditions from the question that
  could NOT be applied: say so in the first sentence ("роль «X» в базе не
  различается — цифры по всем вакансиям Backend") and never present the numbers
  as if they were filtered by them.
- count.vacancies — all matching vacancies over the period, including closed
  (archived) ones; count.active — those still open now. Never call
  count.vacancies "открытые" / "сейчас ищут"; say "за период" and give active
  separately when the question is about now.
- trend: a month with partial=true is not over yet — do not compare its
  vacancy count with full months and do not call it a drop.
- lifetime is computed over Telegram vacancies only (telegram_vacancies).
- Do not print vacancy ids from examples.
- If the numbers are small (fewer than ~10 vacancies, a median from fewer than
  ~8 salaries) say that the data is thin. If FACTS lack what was asked, say so.
- Salaries are monthly, in roubles; write them like "250 тыс. ₽". Shares are
  fractions (0.25 = 25%); premium is a fraction too; delta_pp — percentage points.
- For variants compare them side by side on the same numbers.
- Do not mention sections that are absent from FACTS unless the question asked
  about them.

Format: plain text in Russian, no markdown headers, no tables. 1-2 sentences
with the direct answer, then up to 5 short lines starting with "- ". At most
150 words.
""".strip()

ASK_MARKET_ANSWER_USER_PROMPT = """<QUESTION>
{question}
</QUESTION>

<FACTS>
{facts}
</FACTS>"""


def build_ask_market_plan_messages(
    *, question: str, history_json: str, today: str, roles: str, grades: str, formats: str
) -> list[dict[str, str]]:
    """План запроса к каталогу (JSON) по вопросу и истории диалога."""
    return [
        {"role": "system", "content": ASK_MARKET_PLAN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": ASK_MARKET_PLAN_USER_PROMPT.format(
                today=today, roles=roles, grades=grades, formats=formats, history=history_json, question=question
            ),
        },
    ]


def build_ask_market_answer_messages(*, question: str, facts_json: str) -> list[dict[str, str]]:
    """Ответ на вопрос (обычный текст) только по посчитанным фактам."""
    return [
        {"role": "system", "content": ASK_MARKET_ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": ASK_MARKET_ANSWER_USER_PROMPT.format(question=question, facts=facts_json)},
    ]
