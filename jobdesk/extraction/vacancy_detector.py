"""Правиловая оценка вероятности что пост — вакансия."""

from __future__ import annotations

import re

from jobdesk.extraction.normalizer import normalize_for_match

_STRONG_VACANCY = re.compile(
    r"\b("
    r"вакансия|vacancy|hire|hiring|нанимаем|ищем\s+в\s+команду|открыта\s+позиция|"
    r"open\s+position|looking\s+for|we\s+are\s+looking\s+for|"
    r"требуется|ищем\s+специалист|найм|position\s*\(|"
    r"job\s+opening|работа\s+в\s+коман"
    r")\b",
    re.I,
)

_DEV_WORDS = re.compile(
    r"\b(developer|engineer|разработчик|инженер)\b",
    re.I,
)

# Не вакансия, что бы ни было в тексте: пост соискателя (хэштеги, «ищу работу») или подборка
# чужих вакансий (одна строка на десяток ролей). Текст — после ``normalize_for_match`` (ё → е).
_HARD_EXCLUDE = re.compile(
    r"\bищу\s+(?:работу|вакансию|удаленную\s+работу|проект)\b"
    r"|\bresume\s+cv\b|\bcv\s+attached\b"
    r"|\bподборк[аиуе]\s+(?:\S+\s+)?вакансий\b|\bвакансий\s+дня\b|\bдайджест\s+вакансий\b"
    r"|\bеще\s+\d+\s+ваканси"
    r"|(?<![\w#])#(?:дайджест|digest|подборка)\w*",
    re.I,
)

# Хэштеги соискателя. ``#cv`` бывает и «computer vision» в вакансии — поэтому хэштеги не
# исключают пост, где есть хэштег вакансии (``#вакансия``, ``#vacancy``, ``#hiring``…).
_SEEKER_TAGS = re.compile(
    r"(?<![\w#])#(?:resume|cv|резюме|ищуработу\w*|ищу_работу|opentowork|lookingforjob|lookingforwork)(?!\w)",
    re.I,
)
_VACANCY_TAGS = re.compile(r"(?<![\w#])#(?:вакансия|вакансии|vacancy|vacancies|hiring|job|jobs|работа)(?!\w)", re.I)

# Курсы, школы, менторство — обычно реклама обучения. Но «присылайте резюме» и «оплачиваем
# обучение» есть примерно в каждой пятой настоящей вакансии, поэтому эти слова исключают пост,
# только если в нём нет ни признаков найма (``_STRONG_VACANCY``), ни названия роли (``_ROLE_WORDS``):
# реклама курса, дошедшая до LLM, стоит дешевле потерянной вакансии.
_SOFT_EXCLUDE = re.compile(
    r"\b(?:курс\w*|обучени\w*|школ\w*|bootcamp\w*|ментор\w*|подборк\w*|репост\w*|резюме)\b",
    re.I,
)
# Роль в тексте — только для обхода ``_SOFT_EXCLUDE`` (в баллы не входит): у тестировщиков и
# аналитиков нет слов developer / engineer.
_ROLE_WORDS = re.compile(
    r"\b(?:developer|engineer|разработчик\w*|инженер\w*|программист\w*|тестировщик\w*|тестировани\w*|"
    r"qa|aqa|sdet|tester|analyst|аналитик\w*|devops|sre|architect|архитектор\w*|designer|дизайнер\w*|"
    r"team\s*lead|тимлид\w*|руководител\w*|специалист\w*|backend|frontend|fullstack|full-stack|"
    r"data\s+scientist|product\s+manager|project\s+manager)\b",
    re.I,
)

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_TG = re.compile(r"(?:@|t\.me/)([a-zA-Z][a-zA-Z0-9_]{4,})")

_SALARY_HINT = re.compile(
    r"\b("
    r"\$|€|usd|eur|rub|руб|salary|зарплат|compensation|доход|ставк|rate|"
    r"from\s+\d|от\s+\d|до\s+\d|\d+\s*-\s*\d+\s*[k$€₽]?|"
    r"\d+\s*k\b"
    r")\b",
    re.I,
)

_GRADE_HINT = re.compile(
    r"\b("
    r"intern|trainee|junior|middle|senior|staff|lead|principal|"
    r"мидл|сеньор|ведущ|стаж"
    r")\b",
    re.I,
)

_COMPANY_HINT = re.compile(
    r"\b(компани|company|employer|стартап|startup|клиент\s*:|brand|бренд)\b",
    re.I,
)


def is_not_vacancy_post(raw_text: str) -> bool:
    """Пост соискателя или подборка вакансий — не вакансия независимо от остального текста."""
    return _not_vacancy(normalize_for_match(raw_text))


def _not_vacancy(t: str) -> bool:
    """``t`` — текст после ``normalize_for_match``."""
    if _HARD_EXCLUDE.search(t):
        return True
    return bool(_SEEKER_TAGS.search(t)) and not _VACANCY_TAGS.search(t)


# Сумма весов всех признаков ниже (3+2+2+1+1+2+1). Сырые баллы нормируются на шкалу 0–100 —
# ту же, что у вакансий HH (``VACANCY_SCORE = 100`` в ``hh/mapper``).
_MAX_RAW_SCORE = 12
VACANCY_SCORE_MAX = 100


def _to_scale(raw: int) -> int:
    """Сырые баллы → 0–100. Формулу повторяет миграция ``052_vacancy_score_100``."""
    return min(VACANCY_SCORE_MAX, round(raw * VACANCY_SCORE_MAX / _MAX_RAW_SCORE))


def compute_vacancy_score(raw_text: str) -> tuple[int, str, int]:
    """
    Возвращает ``(score, reason_human, substantive_score)``, обе оценки — по шкале 0–100.

    ``substantive_score`` — баллы **без** бонуса за email или @username (контакт):
    нужен, чтобы не пропускать посты только из‑за одного упоминания контакта.

    Чем выше ``score`` — тем выше уверенность что это объявление о работе работодателя.
    """
    t = normalize_for_match(raw_text)
    score = 0
    substantive = 0
    hints: list[str] = []

    if _not_vacancy(t):
        return 0, "exclude_keywords", 0

    strong = bool(_STRONG_VACANCY.search(raw_text.lower()))
    dev_role = bool(_DEV_WORDS.search(raw_text.lower()))
    if not strong and not _ROLE_WORDS.search(t) and _SOFT_EXCLUDE.search(t):
        return 0, "exclude_keywords", 0

    if strong:
        score += 3
        substantive += 3
        hints.append("vacancy_markers")

    if dev_role:
        score += 2
        substantive += 2
        hints.append("dev_role_words")

    if _SALARY_HINT.search(t):
        score += 2
        substantive += 2
        hints.append("salary_hint")

    if _GRADE_HINT.search(t):
        score += 1
        substantive += 1
        hints.append("grade_hint")

    if _COMPANY_HINT.search(t):
        score += 1
        substantive += 1
        hints.append("company_hint")

    if _EMAIL.search(raw_text) or _TG.search(raw_text.lower()):
        score += 2
        hints.append("contact")

    if re.search(r"\b(remote|hybrid|office|офис|удален|гибрид)\b", t):
        score += 1
        substantive += 1
        hints.append("work_format")

    reason = "+".join(hints) if hints else "baseline"
    return _to_scale(score), reason, _to_scale(substantive)
