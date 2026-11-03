"""Грейд в начале роли («Senior QA») срезается: нормализация ролей и миграция 053."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from tests.conftest import add_legacy_vacancy

from backend.db.migrate import alembic_ini_path
from jobdesk.ai.schema import VacancyAIExtraction
from jobdesk.config.schema import canonical_specializations, split_specializations, strip_grade_prefix


def test_grade_prefix_is_stripped_but_lead_roles_stay():
    assert canonical_specializations(["Senior QA", "QA", "Middle+ Backend", "Sr. Frontend", "Джун QA"]) == [
        "QA",
        "Backend",
        "Frontend",
    ]
    # После среза — синонимы: «Тестировщик» → QA.
    assert canonical_specializations(["Junior Тестировщик"]) == ["QA"]
    assert canonical_specializations(["QA Lead", "Team Lead", "Head of Engineering", "Lead Architect"]) == [
        "QA Lead",
        "Team Lead",
        "Head of Engineering",
        "Lead Architect",
    ]
    # Одно слово грейда и слова, которые им только начинаются, — не грейд в роли.
    assert strip_grade_prefix("Senior") == "Senior"
    assert strip_grade_prefix("Middleware Engineer") == "Middleware Engineer"


def test_graded_developer_role_splits_like_plain_one():
    assert split_specializations(["Senior Python Developer"]) == split_specializations(["Python Developer"])
    assert split_specializations(["Middle Automation QA", "Python"]) == (["Automation QA"], ["Python"])


def test_llm_answer_drops_grade_from_role():
    ai = VacancyAIExtraction.model_validate({"is_vacancy": True, "detected_specializations": ["Senior QA"]})
    assert ai.detected_specializations == ["QA"]


def test_migration_strips_grade_from_roles(pg_empty_db):
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "052_vacancy_score_100")

    rows = {
        "v1": ["Senior QA", "QA"],
        "v2": ["Middle+ Automation QA", "Python"],
        "v3": ["Junior Тестировщик"],
        "v4": ["QA Lead", "Team Lead"],
        "v5": ["Senior"],
    }
    engine = create_engine(pg_empty_db)
    with Session(engine) as s, s.begin():
        for vid, specs in rows.items():
            add_legacy_vacancy(
                s,
                id=vid,
                source_post_key=vid,
                source_channel="hh",
                source_url="https://hh.ru",
                raw_text="x",
                cleaned_text="x",
                detected_specializations=specs,
                detected_grade="unknown",
                technologies=[],
                application_urls=[],
                extraction_source="hh_api",
                vacancy_score=0,
                vacancy_substantive_score=0,
                is_duplicate=False,
            )
        s.execute(
            text(
                "INSERT INTO saved_filters (name, params) VALUES"
                ' (\'a\', \'{"specializations": ["Senior QA", "QA"], "page": 1}\'),'
                " ('b', '{\"specializations\": [\"QA Lead\"]}')"
            )
        )
        s.execute(text("UPDATE vacancies SET updated_at = now() - interval '1 day'"))

    command.upgrade(cfg, "053_strip_grade_from_roles")
    with engine.connect() as conn:
        specs = dict(conn.execute(text("SELECT id, detected_specializations FROM vacancies")).all())
        assert specs == {
            "v1": ["QA"],
            "v2": ["Automation QA", "Python"],
            "v3": ["QA"],
            "v4": ["QA Lead", "Team Lead"],
            "v5": ["Senior"],
        }
        # Изменённые вакансии переиндексируются: роль входит в текст эмбеддинга.
        touched = {
            r[0] for r in conn.execute(text("SELECT id FROM vacancies WHERE updated_at > now() - interval '1 hour'"))
        }
        assert touched == {"v1", "v2", "v3"}
        params = dict(conn.execute(text("SELECT name, params FROM saved_filters")).all())
        assert params == {
            "a": {"specializations": ["QA"], "page": 1},
            "b": {"specializations": ["QA Lead"]},
        }
    engine.dispose()
