"""Матчинг резюме: CRUD резюме, vacancy_matches, join в list_vacancies, промпт."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.llm_job_helpers import await_llm_job, use_ai_config
from tests.test_solo_match import _add_vacancy, _auth, _register

from backend.db import repository as repo
from backend.db.models import VacancyMatchRow, VacancyRow
from backend.db.session import session_scope
from backend.schemas import VacancyQuery
from backend.services import llm_result_freshness, match_service
from jobdesk.ai.clients.openrouter import _parse_match
from jobdesk.ai.prompts import MATCH_PROMPT_VERSION, build_match_messages


def _vacancy(vid: str, text: str, score: int = 0) -> VacancyRow:
    return VacancyRow(
        id=vid,
        source_post_key=f"ch-{vid}",
        source_channel="ch",
        source_url="https://t.me/s/ch",
        post_date=datetime(2026, 9, 1, tzinfo=UTC),
        raw_text=text,
        cleaned_text=text,
        vacancy_score=score,
    )


def test_resumes_crud(db_session):
    assert repo.list_resumes(db_session) == []

    r = repo.create_resume(db_session, name="Python dev", text="5 лет Python, Django, FastAPI")
    db_session.commit()

    rows = repo.list_resumes(db_session)
    assert [x.id for x in rows] == [r.id]
    assert rows[0].name == "Python dev"

    assert repo.delete_resume(db_session, r.id) is True
    assert repo.delete_resume(db_session, r.id) is False
    assert repo.list_resumes(db_session) == []


def _resume_with_llm_results(db_session):
    resume = repo.create_resume(db_session, name="r", text="python")
    db_session.add(_vacancy("a", "Python dev"))
    db_session.commit()
    repo.upsert_vacancy_match(db_session, resume_id=resume.id, vacancy_id="a", **_match_kwargs(88))
    repo.upsert_cover_letter(db_session, resume_id=resume.id, vacancy_id="a", text="Письмо", tone="neutral", model="m1")
    db_session.commit()
    return resume


def test_update_resume_text_drops_matches_and_letters(db_session):
    """Матч и письмо посчитаны по старому тексту — после правки их нельзя отдавать из кэша."""
    resume = _resume_with_llm_results(db_session)

    repo.update_resume(db_session, resume.id, text="python, go, kubernetes")
    db_session.commit()

    assert repo.get_vacancy_match(db_session, resume_id=resume.id, vacancy_id="a") is None
    assert repo.get_cover_letter(db_session, resume_id=resume.id, vacancy_id="a") is None


@pytest.mark.parametrize("update", [{"name": "Новое имя"}, {"name": "r", "text": "python"}])
def test_update_resume_keeps_results_when_text_unchanged(db_session, update):
    resume = _resume_with_llm_results(db_session)

    repo.update_resume(db_session, resume.id, **update)
    db_session.commit()

    assert repo.get_vacancy_match(db_session, resume_id=resume.id, vacancy_id="a") is not None
    assert repo.get_cover_letter(db_session, resume_id=resume.id, vacancy_id="a") is not None


def _match_kwargs(score: int, **over) -> dict:
    base = dict(
        overall_score=score,
        confidence=80,
        breakdown={
            "role_match": 90,
            "skills_match": 85,
            "seniority_match": 80,
            "domain_match": 100,
            "work_format_match": 100,
        },
        explanation="Совпадение",
        matching_skills=[
            {
                "skill": "python",
                "importance": "critical",
                "evidence": "В резюме есть Python.",
            }
        ],
        missing_skills=[
            {
                "skill": "k8s",
                "importance": "important",
                "evidence": "Опыт с Kubernetes в резюме не указан.",
            }
        ],
        strengths=["Совпадает основной стек."],
        risks=["Опыт с Kubernetes в резюме не указан."],
        model="m1",
    )
    base.update(over)
    return base


def test_match_upsert_and_join_in_list_vacancies(db_session):
    resume = repo.create_resume(db_session, name="r", text="python")
    db_session.add_all(
        [
            _vacancy("a", "Python dev", 10),
            _vacancy("b", "Java dev", 5),
            _vacancy("c", "Go dev", 1),
        ]
    )
    db_session.commit()

    repo.upsert_vacancy_match(
        db_session,
        resume_id=resume.id,
        vacancy_id="a",
        **_match_kwargs(88, explanation="Сильное совпадение"),
    )
    repo.upsert_vacancy_match(
        db_session,
        resume_id=resume.id,
        vacancy_id="c",
        **_match_kwargs(40, explanation="Слабое"),
    )
    db_session.commit()

    items, total = repo.list_vacancies(db_session, VacancyQuery(resume_id=resume.id, sort_by="match_score"))
    assert total == 3
    assert [v.id for v in items] == ["a", "c", "b"]  # nulls last
    assert items[0].match_score == 88
    assert items[0].match_explanation == "Сильное совпадение"
    assert items[2].match_score is None

    # upsert обновляет существующую пару
    repo.upsert_vacancy_match(
        db_session,
        resume_id=resume.id,
        vacancy_id="a",
        **_match_kwargs(55, explanation="Переоценено", model="m2"),
    )
    db_session.commit()
    items, _ = repo.list_vacancies(db_session, VacancyQuery(resume_id=resume.id))
    assert next(v for v in items if v.id == "a").match_score == 55


def test_match_prompt_and_parse():
    assert MATCH_PROMPT_VERSION
    messages = build_match_messages(resume_text="Python, FastAPI", vacancy_text="Нужен Python-разработчик")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "<CANDIDATE_RESUME>" in messages[1]["content"]
    assert "Python, FastAPI" in messages[1]["content"]
    assert "<VACANCY>" in messages[1]["content"]
    assert "Нужен Python-разработчик" in messages[1]["content"]
    # Все текстовые поля — по-русски; футер Telegram-канала — не требования.
    assert "evidence, strengths, risks, explanation: Russian" in messages[0]["content"]
    assert "channel footers" in messages[0]["content"]
    assert "canonical Latin spelling everywhere" in messages[0]["content"]  # «питон» → Python в skill

    m = _parse_match(
        '{"confidence": 91,'
        ' "breakdown": {"role_match": 90, "skills_match": 75, "seniority_match": 85,'
        '               "domain_match": 70, "work_format_match": 100},'
        ' "matching_skills": [{"skill": "Python", "importance": "critical",'
        '                     "evidence": "В резюме указан опыт с Python."}],'
        ' "missing_skills": [{"skill": "Kubernetes", "importance": "important",'
        '                    "evidence": "Опыт с Kubernetes в резюме не указан."}],'
        ' "strengths": ["Совпадает основная backend-роль."],'
        ' "risks": ["Опыт с Kubernetes в резюме не указан."],'
        ' "explanation": "Кандидат соответствует роли."}'
    )
    assert m.confidence == 91
    assert m.breakdown.role_match == 90
    assert m.matching_skills[0].skill == "Python"
    assert m.matching_skills[0].importance == "critical"
    assert m.missing_skills[0].evidence.endswith("не указан.")

    fenced = _parse_match('```json\n{"confidence": 30}\n```')
    assert fenced.confidence == 30
    assert fenced.matching_skills == []

    with pytest.raises(json.JSONDecodeError):
        _parse_match("not json")


def test_match_extraction_tolerates_sloppy_llm_output():
    """Строки вместо объектов, float/str оценки, лишние элементы — нормализуются."""
    m = _parse_match(
        '{"confidence": "85.7",'
        ' "breakdown": {"role_match": 88.4, "skills_match": "70"},'
        ' "matching_skills": ["python", "fastapi"],'
        ' "missing_skills": [{"skill": "k8s", "importance": "must_have", "evidence": null}],'
        ' "strengths": ["a", "b", "c", "d", "e", "f"],'
        ' "risks": "один риск",'
        ' "explanation": null}'
    )
    assert m.confidence == 86
    assert m.breakdown.role_match == 88
    assert m.breakdown.skills_match == 70
    assert m.breakdown.domain_match == 0  # не передан — дефолт
    assert m.matching_skills[0].skill == "python"
    assert m.matching_skills[0].importance == "important"
    assert m.missing_skills[0].importance == "critical"  # must_have → critical
    assert m.missing_skills[0].evidence == ""
    assert len(m.strengths) == 5
    assert m.risks == ["один риск"]
    assert m.explanation == ""


def test_calculate_overall_score_weights():
    from jobdesk.ai.schema import (
        MatchBreakdown,
        calculate_overall_score,
    )

    breakdown = MatchBreakdown(
        role_match=90,
        skills_match=75,
        seniority_match=85,
        domain_match=70,
        work_format_match=100,
    )
    # 90*0.3 + 75*0.35 + 85*0.2 + 70*0.1 + 100*0.05 = 82.25 → 82
    assert calculate_overall_score(breakdown, []) == 82


def test_calculate_overall_score_critical_penalty():
    from jobdesk.ai.schema import (
        MatchBreakdown,
        SkillMatch,
        calculate_overall_score,
    )

    strong = MatchBreakdown(
        role_match=95,
        skills_match=95,
        seniority_match=95,
        domain_match=100,
        work_format_match=100,
    )
    one_critical = [SkillMatch(skill="Java", importance="critical")]
    two_critical = [*one_critical, SkillMatch(skill="Spring Boot", importance="critical")]
    important_only = [SkillMatch(skill="Kubernetes", importance="important")]

    assert calculate_overall_score(strong, important_only) == 96
    assert calculate_overall_score(strong, one_critical) == 69
    assert calculate_overall_score(strong, two_critical) == 59
    # penalty не повышает низкий score
    weak = MatchBreakdown(role_match=20, skills_match=20, seniority_match=20)
    assert calculate_overall_score(weak, two_critical) == 17


# --- Актуальность сохранённой оценки (модель матчинга / версия промпта) ---


@pytest.fixture
def counting_match_llm(monkeypatch):
    """Реальный ``match_single`` с подменённым клиентом: считает вызовы LLM."""
    from jobdesk.ai.clients.openrouter import LLMUsage
    from jobdesk.ai.schema import VacancyMatchExtraction

    calls: list[str] = []

    class _FakeClient:
        def match_with_fallback(self, *, resume_text, vacancy_text):
            calls.append(vacancy_text)
            return VacancyMatchExtraction(confidence=80, explanation="ok"), LLMUsage(model="answered/model")

    monkeypatch.setattr(match_service, "get_interactive_client", lambda: _FakeClient())
    return calls


def _matched_pair(client) -> tuple[str, int, str]:
    token = _register(client, "a@example.com")
    vid = _add_vacancy()
    rid = client.post("/api/v1/resumes", headers=_auth(token), json={"name": "CV", "text": "python"}).json()["id"]
    job = await_llm_job(client, token, client.post(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token)))
    assert job["status"] == "done", job
    return token, rid, vid


def test_fresh_match_served_from_cache(api_client, counting_match_llm, monkeypatch, tmp_path):
    use_ai_config(monkeypatch, tmp_path, match_model="match/a")
    token, rid, vid = _matched_pair(api_client)

    again = api_client.post(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token))
    assert again.status_code == 200
    assert again.json()["stale"] is False
    assert len(counting_match_llm) == 1


def test_match_recomputed_after_match_model_change(api_client, counting_match_llm, monkeypatch, tmp_path):
    use_ai_config(monkeypatch, tmp_path, match_model="match/a")
    token, rid, vid = _matched_pair(api_client)
    use_ai_config(monkeypatch, tmp_path, match_model="match/b")

    got = api_client.get(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token))
    assert got.json()["stale"] is True

    r = api_client.post(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token))
    assert r.status_code == 202
    job = await_llm_job(api_client, token, r)
    assert job["status"] == "done", job
    assert job["match"]["stale"] is False
    assert len(counting_match_llm) == 2

    got = api_client.get(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token))
    assert got.json()["stale"] is False
    with session_scope() as session:
        rows = session.scalars(select(VacancyMatchRow).where(VacancyMatchRow.resume_id == rid)).all()
        assert [(r.requested_model, r.model) for r in rows] == [("match/b", "answered/model")]


def test_match_recomputed_after_prompt_version_bump(api_client, counting_match_llm, monkeypatch):
    token, rid, vid = _matched_pair(api_client)
    monkeypatch.setattr(llm_result_freshness, "MATCH_PROMPT_VERSION", "match-v999")
    monkeypatch.setattr(match_service, "MATCH_PROMPT_VERSION", "match-v999")

    assert api_client.get(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token)).json()["stale"] is True
    r = api_client.post(f"/api/v1/resumes/{rid}/match/{vid}", headers=_auth(token))
    assert r.status_code == 202
    job = await_llm_job(api_client, token, r)
    assert job["match"]["stale"] is False
    assert len(counting_match_llm) == 2


def test_vacancy_list_marks_stale_match_without_changing_score_or_order(
    api_client, counting_match_llm, monkeypatch, tmp_path
):
    use_ai_config(monkeypatch, tmp_path, match_model="match/a")
    token, rid, vid = _matched_pair(api_client)
    _add_vacancy("vac-2", "Нужен Go-разработчик")

    def _list() -> list[tuple]:
        r = api_client.get(
            "/api/v1/vacancies",
            headers=_auth(token),
            params={"resume_id": rid, "sort_by": "match_score"},
        )
        assert r.status_code == 200, r.text
        return [(i["id"], i["match_score"], i["match_stale"]) for i in r.json()["items"]]

    before = _list()
    assert before == [(vid, before[0][1], False), ("vac-2", None, None)]

    use_ai_config(monkeypatch, tmp_path, match_model="match/b")
    assert _list() == [(vid, before[0][1], True), ("vac-2", None, None)]
