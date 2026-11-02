"""Технологии отдельно от специализаций: каталог, разделение, фильтры, миграция 037."""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from backend.db.migrate import alembic_ini_path
from backend.schemas import VacancyQuery
from jobdesk.ai.schema import VacancyAIExtraction
from jobdesk.config.schema import (
    DEFAULT_SPECIALIZATIONS,
    AppConfig,
    FiltersConfig,
    canonical_technologies,
    split_specializations,
)
from jobdesk.filters.vacancy_filters import apply_post_filters, matches_technology_filter
from jobdesk.ingestion.rules import rules_from_raw_post
from jobdesk.models.vacancy import Vacancy
from jobdesk.parsers.base import RawPost


def test_languages_are_not_specializations():
    for lang in ("Python", "Java", "Go", "Golang"):
        assert lang not in DEFAULT_SPECIALIZATIONS


def test_split_moves_languages_into_technologies():
    assert split_specializations(["Automation QA", "Python"]) == (["Automation QA"], ["Python"])
    assert split_specializations(["AI", "Python"], ["PyTorch"]) == (["AI"], ["PyTorch", "Python"])
    assert split_specializations(["Golang", "Java"]) == ([], ["Go", "Java"])
    # Составные и «<язык> developer»: роль разработчика остаётся общей.
    assert split_specializations(["C#/.NET"]) == ([], ["C#", ".NET"])
    assert split_specializations(["Python Developer"]) == (["Software Engineer"], ["Python"])
    assert split_specializations(["Программист 1С", "Backend"]) == (["Backend"], ["1С"])
    # Роли, похожие на «X engineer», не трогаются; старые имена ролей — канонические.
    roles, techs = split_specializations(["QA Engineer", "Data Engineer", "Android", "iOS", "AQA"])
    assert roles == ["QA Engineer", "Data Engineer", "Android", "iOS", "Automation QA"]
    assert techs == []


def test_canonical_technologies_aliases_case_and_dedup():
    assert canonical_technologies(["golang", "Go", "python", "Apache Kafka", "REST API", "1c", "Bazel"]) == [
        "Go",
        "Python",
        "Kafka",
        "REST",
        "1С",
        "Bazel",  # неизвестная — как есть
    ]
    assert canonical_technologies(["Foo", "foo", " ", ""]) == ["Foo"]


def test_llm_answer_with_language_role_is_split():
    ai = VacancyAIExtraction.model_validate(
        {"detected_specializations": ["Automation QA", "Python"], "technologies": ["pytest", "Golang"]}
    )
    assert ai.detected_specializations == ["Automation QA"]
    assert ai.technologies == ["Pytest", "Go", "Python"]


def test_filters_config_moves_languages_to_technologies():
    f = FiltersConfig(specializations=["Python", "QA", "AQA", "Go", "Golang"])
    assert f.specializations == ["QA", "Automation QA"]
    assert f.technologies == ["Python", "Go"]


def test_vacancy_query_moves_languages_from_old_links():
    q = VacancyQuery(specializations=["Python", "Backend"], technologies=["golang"])
    assert q.specializations == ["Backend"]
    assert q.technologies == ["Go", "Python"]


def test_rules_put_language_into_technologies(default_test_config):
    from jobdesk.config.loader import load_config

    cfg = load_config(default_test_config)
    post = RawPost(
        channel_username="chan",
        post_id=1,
        text="Ищем AQA, стек Python + pytest, удалёнка",
        post_date=datetime(2026, 10, 1, tzinfo=UTC),
        post_url=None,
    )
    v = rules_from_raw_post(post, cfg)
    assert v.detected_specializations == ["Automation QA"]
    assert {"Python", "Pytest"} <= set(v.technologies)


def _v(specs: list[str], techs: list[str]) -> Vacancy:
    return Vacancy(
        id="x",
        source_post_key="c-1",
        source_channel="c",
        source_url="https://t.me/s/c",
        raw_text="x",
        cleaned_text="x",
        detected_specializations=specs,
        technologies=techs,
        vacancy_score=83,
        vacancy_substantive_score=83,
    )


def test_technology_post_filter_is_and_with_specializations():
    assert matches_technology_filter(_v([], ["Python"]), [])
    assert matches_technology_filter(_v([], ["Python"]), ["python", "Go"])
    assert not matches_technology_filter(_v([], ["Java"]), ["Python"])

    cfg = AppConfig(filters=FiltersConfig(specializations=["Automation QA"], technologies=["Python"]))
    assert apply_post_filters(_v(["Automation QA"], ["Python"]), cfg)
    assert not apply_post_filters(_v(["Automation QA"], ["Java"]), cfg)
    assert not apply_post_filters(_v(["Backend"], ["Python"]), cfg)
    # any_specialization снимает только фильтр ролей, технологии — остаются.
    any_cfg = AppConfig(filters=FiltersConfig(any_specialization=True, technologies=["Python"]))
    assert apply_post_filters(_v(["Backend"], ["Python"]), any_cfg)
    assert not apply_post_filters(_v(["Backend"], ["Go"]), any_cfg)


def test_migration_separates_technologies(pg_empty_db):
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "036_merge_qa_specializations")

    rows = {
        "v1": ('["Automation QA", "Python"]', '["pytest", "Python"]'),
        "v2": ('["Go"]', '["Golang", "Kubernetes"]'),
        "v3": ('["Python Developer"]', "[]"),
        "v4": ('["Backend", "C#/.NET"]', '["Apache Kafka"]'),
        "v5": ('["QA"]', '["Selenium"]'),
    }
    engine = create_engine(pg_empty_db)
    with engine.begin() as conn:
        for vid, (specs, techs) in rows.items():
            conn.execute(
                text(
                    "INSERT INTO vacancies (id, source_post_key, source_channel, source_url, raw_text,"
                    " cleaned_text, detected_specializations, detected_grade, technologies, application_urls,"
                    " extraction_source, vacancy_score, vacancy_substantive_score, is_duplicate, parsed_at,"
                    " updated_at) VALUES (:id, :id, 'chan', 'https://t.me/s/chan', 'x', 'x',"
                    " CAST(:s AS jsonb), 'unknown', CAST(:t AS json), '[]', 'ai', 0, 0, false, now(), now())"
                ),
                {"id": vid, "s": specs, "t": techs},
            )
        conn.execute(
            text(
                "INSERT INTO saved_filters (name, params) VALUES"
                ' (\'a\', \'{"specializations": ["Python", "QA"], "page": 1}\'),'
                " ('b', '{\"specializations\": [\"Backend\"]}'), ('c', '{\"grade\": null}')"
            )
        )

    command.upgrade(cfg, "037_separate_technologies")
    with engine.connect() as conn:
        got = {
            vid: (specs, techs)
            for vid, specs, techs in conn.execute(
                text("SELECT id, detected_specializations, technologies FROM vacancies")
            ).all()
        }
        assert got == {
            "v1": (["Automation QA"], ["Pytest", "Python"]),
            "v2": ([], ["Go", "Kubernetes"]),
            "v3": (["Software Engineer"], ["Python"]),
            "v4": (["Backend"], ["Kafka", "C#", ".NET"]),
            "v5": (["QA"], ["Selenium"]),
        }
        col_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns WHERE table_name='vacancies' AND column_name='technologies'"
            )
        ).scalar()
        assert col_type == "jsonb"
        assert conn.execute(text("SELECT to_regclass('ix_vacancies_technologies_gin')")).scalar() is not None
        params = dict(conn.execute(text("SELECT name, params FROM saved_filters")).all())
        assert params == {
            "a": {"specializations": ["QA"], "technologies": ["Python"], "page": 1},
            "b": {"specializations": ["Backend"]},
            "c": {"grade": None},
        }
    engine.dispose()


def test_one_c_variants_collapse_into_one_technology():
    names = ["1С:Предприятие 8.3", "1C:ERP", "1С КА", "язык запросов 1С", "1C", "Python"]
    assert canonical_technologies(names) == ["1С", "Python"]
    # Битрикс — отдельный продукт; слово, где «1с» не отдельный токен, не трогается.
    assert canonical_technologies(["1С-Битрикс", "Битрикс24", "1Сник"]) == ["Bitrix", "1Сник"]
    # Для ролей правило не действует: «Руководитель отдела 1С» остаётся ролью.
    assert split_specializations(["Руководитель отдела 1С"]) == (["Руководитель отдела 1С"], [])


def test_migration_collapses_1c_technologies(pg_empty_db):
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "037_separate_technologies")

    rows = {
        "v1": '["1С:Предприятие 8.3", "1C:ERP", "SQL", "1С"]',
        "v2": '["1C-Bitrix", "Битрикс24", "PHP"]',
        "v3": '["Python"]',
    }
    engine = create_engine(pg_empty_db)
    with engine.begin() as conn:
        for vid, techs in rows.items():
            conn.execute(
                text(
                    "INSERT INTO vacancies (id, source_post_key, source_channel, source_url, raw_text,"
                    " cleaned_text, detected_specializations, detected_grade, technologies, application_urls,"
                    " extraction_source, vacancy_score, vacancy_substantive_score, is_duplicate, parsed_at,"
                    " updated_at) VALUES (:id, :id, 'chan', 'https://t.me/s/chan', 'x', 'x',"
                    " '[]', 'unknown', CAST(:t AS jsonb), '[]', 'ai', 0, 0, false, now(), now())"
                ),
                {"id": vid, "t": techs},
            )

    command.upgrade(cfg, "038_collapse_1c_technologies")
    with engine.connect() as conn:
        got = dict(conn.execute(text("SELECT id, technologies FROM vacancies")).all())
    engine.dispose()
    assert got == {"v1": ["1С", "SQL"], "v2": ["Bitrix", "PHP"], "v3": ["Python"]}
