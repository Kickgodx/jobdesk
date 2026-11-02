"""Промпт extraction: компания, контакт, опыт и занятость; нормализация занятости."""

from __future__ import annotations

import pytest

from jobdesk.ai.prompts import SYSTEM_PROMPT_V1, build_messages
from jobdesk.ai.schema import VacancyAIExtraction, normalize_employment_type
from jobdesk.ai.search_context import SearchContext


def _few_shot_user() -> str:
    ctx = SearchContext(
        target_specializations=[],
        target_work_formats=["any"],
        exclude_keywords=[],
        allowed_specializations=["QA"],
        allowed_work_formats=["remote", "hybrid", "office", "relocation", "unknown"],
        any_specialization=True,
    )
    return build_messages(text="Hiring: Dev", search_context=ctx, use_few_shot=True)[1]["content"]


def test_system_prompt_defines_employer_fields():
    for field in ("company", "contact", "required_experience", "employment_type"):
        assert f"- {field} —" in SYSTEM_PROMPT_V1
    assert "NOT the Telegram channel" in SYSTEM_PROMPT_V1
    assert "staffing agency" in SYSTEM_PROMPT_V1
    assert "full-time, part-time, contract" in SYSTEM_PROMPT_V1


def test_few_shot_shows_employer_fields_and_channel_footer():
    user = _few_shot_user()
    assert '"company": "PayLab"' in user
    assert '"contact": "@paylab_hr"' in user
    assert '"employment_type": "contract"' in user  # freelance hourly
    assert "@it_jobs_feed" in user  # футер канала в тексте, но не в contact
    assert '"contact": "@it_jobs_feed"' not in user


def test_few_shot_consistent_with_rules():
    user = _few_shot_user()
    assert "директора" not in user
    # «Engineer II» без слова грейда и опыта → unknown, как требует правило грейда.
    eur = user.split("Quality Assurance Engineer II", 1)[1].split("\n\n", 1)[0]
    assert '"detected_grade": "unknown"' in eur


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Full-time", "full-time"),
        ("full_time", "full-time"),
        ("Полная занятость", "full-time"),
        ("part time", "part-time"),
        ("Частичная", "part-time"),
        ("freelance", "contract"),
        ("ГПХ", "contract"),
        ("Contractor", "contract"),
        ("internship", None),
        ("гибкий график", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_employment_type(raw, expected):
    assert normalize_employment_type(raw) == expected


def test_schema_normalizes_employment_type():
    assert VacancyAIExtraction(employment_type="Full time").employment_type == "full-time"
    assert VacancyAIExtraction(employment_type=["ГПХ"]).employment_type == "contract"
    assert VacancyAIExtraction(employment_type="5/2, офис").employment_type is None
