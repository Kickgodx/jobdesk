"""Пост-фильтры вакансий (jobdesk.filters.vacancy_filters)."""

from __future__ import annotations

import pytest

from jobdesk.config.schema import AppConfig
from jobdesk.filters.vacancy_filters import (
    apply_post_filters,
    matches_exclude_keywords,
    matches_location_policy,
    matches_specialization_filter,
    matches_work_format_policy,
)
from jobdesk.models.vacancy import Vacancy


def _vacancy(text: str = "Python backend developer, Django, PostgreSQL", **kwargs) -> Vacancy:
    fields = {
        "id": "v1",
        "source_post_key": "ch-1",
        "source_channel": "ch",
        "source_url": "https://t.me/s/ch",
        "raw_text": text,
        "cleaned_text": text,
        "vacancy_score": 83,
        "vacancy_substantive_score": 42,
    }
    fields.update(kwargs)
    return Vacancy(**fields)


# --- специализации ---


def test_specialization_empty_selection_allows_everything():
    assert matches_specialization_filter(_vacancy(detected_specializations=[]), []) is True


def test_specialization_requires_intersection():
    v = _vacancy(detected_specializations=["python", "devops"])

    assert matches_specialization_filter(v, ["go", "devops"]) is True
    assert matches_specialization_filter(v, ["go", "java"]) is False


def test_specialization_selected_but_nothing_detected_is_rejected():
    assert matches_specialization_filter(_vacancy(detected_specializations=[]), ["python"]) is False


# --- исключающие слова ---


def test_exclude_keyword_substring_rejects():
    assert matches_exclude_keywords(_vacancy("Ищем стажёра-разработчика"), ["стажёр"]) is False


def test_exclude_keyword_normalizes_case_and_yo():
    # normalize_for_match: lowercase + «ё» → «е» с обеих сторон.
    assert matches_exclude_keywords(_vacancy("СТАЖЕР в команду"), ["Стажёр"]) is False


def test_exclude_keyword_absent_passes():
    assert matches_exclude_keywords(_vacancy(), ["стажёр", "sales"]) is True


def test_short_exclude_keyword_matches_whole_word_only():
    """Ключи ≤ 2 символов («hr») — только целым словом, иначе режут «chrome», «through»."""
    assert matches_exclude_keywords(_vacancy("Frontend, Chrome DevTools, go through code review"), ["hr"]) is True
    assert matches_exclude_keywords(_vacancy("Ищем HR-менеджера"), ["hr"]) is False
    assert matches_exclude_keywords(_vacancy("Вакансия: hr"), ["HR"]) is False


def test_short_exclude_keyword_cyrillic_word_boundary():
    assert matches_exclude_keywords(_vacancy("Работа в регионе, гибкий график"), ["ио"]) is True
    assert matches_exclude_keywords(_vacancy("Должность: ио руководителя"), ["ио"]) is False


def test_blank_exclude_keywords_are_ignored():
    assert matches_exclude_keywords(_vacancy(), ["", "   "]) is True


# --- формат работы ---


@pytest.mark.parametrize("formats", [[], ["any"], ["remote", "any"]])
def test_work_format_no_restriction(formats):
    assert matches_work_format_policy(_vacancy(work_format="office"), formats) is True


@pytest.mark.parametrize(
    ("work_format", "expected"),
    [("remote", True), ("hybrid", True), ("office", False), ("relocation", False)],
)
def test_work_format_allowed_list(work_format, expected):
    assert matches_work_format_policy(_vacancy(work_format=work_format), ["remote", "hybrid"]) is expected


def test_work_format_unknown_passes_allowed_list():
    """Формат не распознан — не отсекаем: правила часто не находят его в тексте."""
    assert matches_work_format_policy(_vacancy(work_format=None), ["remote"]) is True


@pytest.mark.parametrize(
    ("work_format", "expected"),
    [("office", False), ("remote", True), ("hybrid", True), (None, True)],
)
def test_exclude_office_alone(work_format, expected):
    assert matches_work_format_policy(_vacancy(work_format=work_format), ["exclude_office"]) is expected


def test_exclude_office_combined_with_allowed_list():
    formats = ["exclude_office", "remote"]

    assert matches_work_format_policy(_vacancy(work_format="office"), formats) is False
    assert matches_work_format_policy(_vacancy(work_format="remote"), formats) is True
    assert matches_work_format_policy(_vacancy(work_format="hybrid"), formats) is False
    assert matches_work_format_policy(_vacancy(work_format=None), formats) is True


# --- локация ---


def test_location_empty_lists_allow_everything():
    assert matches_location_policy(_vacancy(country="Германия", city="Берлин"), [], []) is True


def test_location_country_field_match_is_case_insensitive():
    assert matches_location_policy(_vacancy(country="РОССИЯ"), ["россия"], []) is True


def test_location_country_partial_match_both_directions():
    # Поле длиннее фильтра и наоборот.
    assert matches_location_policy(_vacancy(country="Россия (РФ)"), ["Россия"], []) is True
    assert matches_location_policy(_vacancy(country="Росс"), ["Россия"], []) is True


def test_location_country_found_in_text():
    v = _vacancy("Python developer, офис в Казахстане", country="Неизвестно")

    assert matches_location_policy(v, ["Казахстан"], []) is True


@pytest.mark.parametrize("country", [None, "", "   "])
def test_location_unknown_country_passes(country):
    """None и пустая строка одинаково значат «страна неизвестна» — не отсекаем."""
    assert matches_location_policy(_vacancy(country=country), ["Россия"], []) is True


@pytest.mark.parametrize("city", [None, "", "   "])
def test_location_unknown_city_passes(city):
    assert matches_location_policy(_vacancy(city=city), [], ["Москва"]) is True


def test_location_other_country_rejected():
    assert matches_location_policy(_vacancy(country="Германия"), ["Россия", "Казахстан"], []) is False


def test_location_city_rules():
    assert matches_location_policy(_vacancy(city="Москва"), [], ["Москва", "Казань"]) is True
    assert matches_location_policy(_vacancy("Офис в центре Казань", city="Татарстан"), [], ["Казань"]) is True
    assert matches_location_policy(_vacancy(city="Новосибирск"), [], ["Москва"]) is False


def test_location_country_and_city_both_must_pass():
    v = _vacancy(country="Россия", city="Новосибирск")

    assert matches_location_policy(v, ["Россия"], ["Новосибирск"]) is True
    assert matches_location_policy(v, ["Россия"], ["Москва"]) is False
    assert matches_location_policy(v, ["Германия"], ["Новосибирск"]) is False


# --- apply_post_filters ---


def _config(**filters) -> AppConfig:
    cfg = AppConfig()
    for key, value in filters.items():
        setattr(cfg.filters, key, value)
    return cfg


def test_apply_post_filters_passes_clean_vacancy():
    v = _vacancy(detected_specializations=["python"], work_format="remote", country="Россия", city="Москва")
    cfg = _config(specializations=["python"], work_formats=["remote"], countries=["Россия"], cities=["Москва"])

    assert apply_post_filters(v, cfg) is True


def test_apply_post_filters_score_threshold():
    cfg = AppConfig()
    threshold = cfg.parser.vacancy_score_threshold

    assert apply_post_filters(_vacancy(vacancy_score=threshold), cfg) is True
    assert apply_post_filters(_vacancy(vacancy_score=threshold - 1), cfg) is False


def test_apply_post_filters_substantive_score_threshold():
    cfg = AppConfig()
    cfg.parser.vacancy_min_substantive_score = 17

    assert apply_post_filters(_vacancy(vacancy_substantive_score=17), cfg) is True
    assert apply_post_filters(_vacancy(vacancy_substantive_score=8), cfg) is False


@pytest.mark.parametrize(
    ("vacancy_kwargs", "filters"),
    [
        ({"detected_specializations": ["java"]}, {"specializations": ["python"]}),
        ({"work_format": "office"}, {"work_formats": ["remote"]}),
        ({"country": "Германия"}, {"countries": ["Россия"]}),
        ({"city": "Берлин"}, {"cities": ["Москва"]}),
    ],
    ids=["specialization", "work_format", "country", "city"],
)
def test_apply_post_filters_each_filter_can_reject(vacancy_kwargs, filters):
    assert apply_post_filters(_vacancy(**vacancy_kwargs), _config(**filters)) is False


def test_apply_post_filters_uses_merged_exclude_keywords_by_default():
    """Без явного списка берутся словарь по умолчанию + filters.exclude_keywords."""
    cfg = _config(exclude_keywords=["1с-битрикс"])

    assert apply_post_filters(_vacancy("Junior Python developer"), cfg) is False  # из DEFAULT_EXCLUDE_KEYWORDS
    assert apply_post_filters(_vacancy("Python, 1С-Битрикс"), cfg) is False  # из конфига
    assert apply_post_filters(_vacancy(), cfg) is True


def test_apply_post_filters_explicit_exclude_keywords_override_config():
    """Переданный список заменяет словарь конфига (пакетная обработка собирает его один раз)."""
    cfg = AppConfig()
    junior = _vacancy("Junior Python developer")

    assert apply_post_filters(junior, cfg, exclude_keywords=[]) is True
    assert apply_post_filters(_vacancy(), cfg, exclude_keywords=["django"]) is False


def test_apply_post_filters_any_specialization_skips_specialization_filter():
    v = _vacancy(detected_specializations=["QA"])
    cfg = _config(specializations=["Python"], any_specialization=True)

    assert apply_post_filters(v, cfg) is True
    cfg.filters.any_specialization = False
    assert apply_post_filters(v, cfg) is False
