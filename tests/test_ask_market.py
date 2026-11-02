"""«Спросить базу»: план → фильтры каталога → факты → ответ; джоба, квота, история."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.llm_job_helpers import await_llm_job

from backend.db.models import VacancyRow
from backend.db.session import session_scope
from backend.services import ask_market_service
from backend.services.ask_market_service import compute_facts, normalize_filters
from backend.settings import get_settings
from jobdesk.ai.clients.openrouter import LLMUsage, OpenRouterClient, _parse_market_plan
from jobdesk.ai.prompts import build_ask_market_answer_messages, build_ask_market_plan_messages
from jobdesk.ai.schema import MarketQueryFilters, MarketQueryPlan
from jobdesk.config.loader import load_config
from jobdesk.config.schema import AiConfig

_n = iter(range(1, 1_000_000))


def _cfg():
    return load_config(get_settings().config_path)


def _vac(**kw) -> VacancyRow:
    i = next(_n)
    defaults = {
        "id": f"am-{i}",
        "source_post_key": f"k-{i}",
        "source_channel": "ch",
        "source_url": "u",
        "post_date": datetime.now(UTC) - timedelta(days=3),
        "raw_text": "t",
        "cleaned_text": "t",
        "detected_specializations": ["Backend"],
        "technologies": ["Python"],
        "detected_grade": "senior",
        "work_format": "remote",
        "company": "Ромашка",
    }
    defaults.update(kw)
    return VacancyRow(**defaults)


# --- План


def test_plan_schema_tolerates_null_filters_and_unknown_topics():
    plan = _parse_market_plan(
        json.dumps(
            {"answerable": False, "reason": "не про рынок", "filters": None, "topics": ["salary", "weather", "salary"]}
        )
    )
    assert plan.filters == MarketQueryFilters()
    assert plan.topics == ["salary"]


def test_plan_uses_strict_schema_and_letters_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = OpenRouterClient(AiConfig(model="parse/model", cover_letter_model="writer/model"))
    seen: dict = {}

    def _fake_call(*, messages, parse, model=None, json_schema=None, **_kw):
        seen.update(model=model, schema=json_schema, user=messages[1]["content"])
        return MarketQueryPlan(), LLMUsage(model=model or "")

    monkeypatch.setattr(client, "_call_llm", _fake_call)
    client.market_plan(
        question="сколько?", history_json="[]", today="2026-10-28", roles="QA", grades="senior", formats="remote"
    )
    assert seen["model"] == "writer/model"
    assert seen["schema"]["additionalProperties"] is False
    assert "TODAY: 2026-10-28" in seen["user"] and "сколько?" in seen["user"]


def test_plan_prompt_mentions_history():
    msgs = build_ask_market_plan_messages(
        question="а для middle?", history_json='[{"question": "q"}]', today="t", roles="r", grades="g", formats="f"
    )
    assert '"question": "q"' in msgs[1]["content"]
    assert "Follow-ups" in msgs[0]["content"]


def test_normalize_filters():
    f, dropped = normalize_filters(
        MarketQueryFilters(
            specializations=["Backend developer", "Python", "Астронавт"],
            technologies=["golang"],
            grade="Sr",
            work_format="удаленка",
            date_from="2026-10-01",
            date_to="2026-09-01",
            has_salary=False,
            text_query="  финтех ",
        ),
        _cfg(),
    )
    assert f["specializations"] == ["Backend"]
    assert f["technologies"] == ["Go", "Python"]
    assert (f["grade"], f["work_format"]) == ("senior", "remote")
    assert (f["date_from"], f["date_to"]) == (date(2026, 9, 1), date(2026, 10, 1))
    assert f["has_salary"] is None  # false от модели — не фильтр
    assert f["q"] == "финтех"
    assert dropped == ["Астронавт"]


# --- Факты


def test_compute_facts_topics_and_variants(db_session):
    db_session.add_all(
        [
            *[_vac(detected_salary_avg_rub=200_000 + 10_000 * i) for i in range(5)],
            _vac(technologies=["Go"], detected_salary_avg_rub=400_000),
            _vac(detected_grade="middle", detected_salary_avg_rub=150_000),
            _vac(archived_at=datetime.now(UTC)),
            _vac(is_duplicate=True, detected_salary_avg_rub=9_000_000),
        ]
    )
    db_session.commit()
    base = {"specializations": ["Backend"], "grade": "senior"}
    facts = compute_facts(
        db_session,
        base,
        ["salary", "companies", "examples"],
        [("Go", {**base, "technologies": ["Go"]}), ("Python", {**base, "technologies": ["Python"]})],
    )
    c = facts["count"]
    assert (c["vacancies"], c["active"], c["with_salary"]) == (7, 6, 6)
    assert c["salary"]["median"] == 225_000
    assert facts["filters"] == {"specializations": ["Backend"], "grade": "senior"}
    assert facts["salary"]["by_grade"][0]["group"] == "senior"
    assert facts["companies"][0] == {"company": "Ромашка", "vacancies": 7, "avg_salary": pytest.approx(250_000, abs=1)}
    assert len(facts["examples"]) == 5  # активные, свежие первыми
    assert "technologies" not in facts and "trend" not in facts
    assert [(v["label"], v["count"]["vacancies"]) for v in facts["variants"]] == [("Go", 1), ("Python", 6)]


def test_trend_marks_current_month_partial(db_session):
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    db_session.add_all([_vac(post_date=now), _vac(post_date=month_start - timedelta(days=40))])
    db_session.commit()
    trend = compute_facts(db_session, {}, ["trend"], [])["trend"]
    assert trend[-1]["month"] == now.strftime("%Y-%m") and trend[-1]["partial"] is True
    assert all("partial" not in m for m in trend[:-1]) and len(trend) == 2


def test_answer_prompt_explains_scope_fields():
    system = build_ask_market_answer_messages(question="q", facts_json="{}")[0]["content"]
    assert "unrecognized_filters" in system
    assert "count.active" in system
    assert "partial=true" in system


# --- Джоба


@pytest.fixture
def fake_llm(monkeypatch):
    calls: dict[str, list] = {"plan": [], "answer": []}

    class _Client:
        def market_plan(self, *, question, history_json, today, roles, grades, formats):
            calls["plan"].append({"question": question, "history": json.loads(history_json), "roles": roles})
            if "собеседован" in question:
                return MarketQueryPlan(answerable=False, reason="Это не про рынок."), LLMUsage(model="plan/m")
            return (
                MarketQueryPlan(
                    filters=MarketQueryFilters(specializations=["бэкенд"], technologies=["Python"]),
                    topics=["salary"],
                ),
                LLMUsage(model="plan/m"),
            )

        def market_answer(self, *, question, facts_json):
            calls["answer"].append(json.loads(facts_json))
            return "Медиана 250 тыс. ₽.", LLMUsage(model="answer/m")

    monkeypatch.setattr(ask_market_service, "get_interactive_client", lambda: _Client())
    return calls


def _token(client, email="a@example.com") -> str:
    return client.post("/api/v1/auth/register", json={"email": email, "password": "secret1"}).json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_ask_job_flow(api_client, fake_llm):
    with session_scope() as s:
        s.add_all([_vac(detected_grade="middle", detected_salary_avg_rub=250_000) for _ in range(3)])
    token = _token(api_client)
    history = [{"question": "Сколько платят senior?", "answer": "…", "filters": {"grade": "senior"}}]
    r = api_client.post(
        "/api/v1/market-chat/ask", headers=_h(token), json={"question": "А middle бэкендерам?", "history": history}
    )
    job = await_llm_job(api_client, token, r)
    assert job["status"] == "done", job
    res = job["ask_market_result"]
    # Грейд, потерянный моделью, — из текста вопроса; роль — по синониму словаря.
    assert res["filters"] == {"specializations": ["Backend"], "technologies": ["Python"], "grade": "middle"}
    assert res["answer"] == "Медиана 250 тыс. ₽." and res["model"] == "answer/m"
    assert res["facts"]["count"]["vacancies"] == 3
    assert fake_llm["plan"][0]["history"][0]["filters"] == {"grade": "senior"}
    assert "Backend" in fake_llm["plan"][0]["roles"]
    assert fake_llm["answer"][0]["salary"]["by_grade"][0]["median"] == 250_000


def test_unanswerable_skips_facts_and_second_call(api_client, fake_llm):
    token = _token(api_client)
    r = api_client.post("/api/v1/market-chat/ask", headers=_h(token), json={"question": "Как пройти собеседование?"})
    res = await_llm_job(api_client, token, r)["ask_market_result"]
    assert (res["answerable"], res["answer"], res["facts"], res["filters"]) == (False, "Это не про рынок.", None, {})
    assert fake_llm["answer"] == []


def test_ask_validation_and_quota(api_client, fake_llm, monkeypatch):
    admin = _token(api_client)  # первый — админ, без квоты
    assert api_client.post("/api/v1/market-chat/ask", headers=_h(admin), json={"question": "?"}).status_code == 422
    too_long = [{"question": "q"}] * 4
    r = api_client.post(
        "/api/v1/market-chat/ask", headers=_h(admin), json={"question": "сколько?", "history": too_long}
    )
    assert r.status_code == 422
    assert api_client.post("/api/v1/market-chat/ask", json={"question": "сколько?"}).status_code == 401

    monkeypatch.setenv("LLM_USER_DAILY_CALLS", "1")
    get_settings.cache_clear()
    user = _token(api_client, "b@example.com")
    first = api_client.post("/api/v1/market-chat/ask", headers=_h(user), json={"question": "сколько вакансий?"})
    assert await_llm_job(api_client, user, first)["status"] == "done"
    second = api_client.post("/api/v1/market-chat/ask", headers=_h(user), json={"question": "а в офисе?"})
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "LLM_QUOTA_EXCEEDED"
