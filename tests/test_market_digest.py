"""Еженедельный обзор рынка: факты недели, текст LLM, хранение, API и задача лидера."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from tests.llm_job_helpers import use_ai_config

from backend.db.models import MarketDigestRow, VacancyRow
from backend.db.session import session_scope
from backend.services import market_digest_service as svc
from jobdesk.ai.clients.openrouter import LLMUsage, OpenRouterClient
from jobdesk.ai.prompts import build_market_digest_messages
from jobdesk.config.schema import AiConfig

WEEK = date(2026, 10, 12)  # понедельник
_n = iter(range(1, 1_000_000))


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _vac(day: date, **kw) -> VacancyRow:
    i = next(_n)
    kw.setdefault("detected_specializations", ["QA"])
    kw.setdefault("technologies", ["Python"])
    return VacancyRow(
        id=f"d-{i}",
        source_post_key=f"k-{i}",
        source_channel=kw.pop("channel", "ch_a"),
        source_url="u",
        post_date=_at(day),
        raw_text="t",
        cleaned_text="t",
        **kw,
    )


def _add(rows) -> None:
    with session_scope() as s:
        s.add_all(rows)


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i % 7) for i in range(n)]


def _catalog() -> None:
    """Неделя: 30 вакансий (Go растёт), прошлая — 20, ещё три недели до неё — по 20 (Java падает)."""
    week = [
        _vac(
            d,
            technologies=["Python", "Go"] if i < 15 else ["Python"],
            detected_grade="middle",
            detected_salary_avg_rub=200_000 + 1000 * i,
            company="ООО Ромашка" if i < 3 else "NewCo",
            company_key="ромашка" if i < 3 else "newco",
            work_format="remote" if i < 20 else "office",
            channel="hh" if i < 10 else "ch_a",
            detected_specializations=["Backend"] if i < 12 else ["QA"],
        )
        for i, d in enumerate(_days(WEEK, 30))
    ]
    prev = [
        _vac(
            d,
            technologies=["Python", "Java"],
            detected_grade="middle",
            detected_salary_avg_rub=150_000,
            company="Ромашка",
            company_key="ромашка",
            work_format="office",
        )
        for d in _days(WEEK - timedelta(days=7), 20)
    ]
    older = [
        _vac(d, technologies=["Python", "Java"], company="Ромашка", company_key="ромашка")
        for w in (14, 21, 28)
        for d in _days(WEEK - timedelta(days=w), 20)
    ]
    excluded = [
        _vac(WEEK, is_duplicate=True, technologies=["Rust"]),
        _vac(WEEK + timedelta(days=7)),  # следующая неделя
    ]
    _add(week + prev + older + excluded)


def test_facts_of_week(pg_db):
    _catalog()
    with session_scope() as s:
        f = svc.compute_facts(s, WEEK)
    assert (f.period_start, f.period_end) == (WEEK, date(2026, 10, 18))
    assert (f.vacancies, f.previous_vacancies, f.avg_4w_vacancies) == (30, 20, 20.0)
    assert f.with_salary_share == 1.0
    assert [(r.name, r.count, r.previous) for r in f.roles] == [("QA", 18, 20), ("Backend", 12, 0)]
    assert [(r.name, r.count, r.previous) for r in f.work_formats] == [("remote", 20, 0), ("office", 10, 20)]
    assert [(r.name, r.count) for r in f.sources] == [("ch_a", 20), ("hh", 10)]

    up = {t.name: t for t in f.technologies_up}
    down = {t.name: t for t in f.technologies_down}
    assert up["Go"].share == 0.5 and up["Go"].delta_pp == 50.0
    assert down["Java"].share == 0.0 and down["Java"].previous_share == 1.0
    assert "Python" not in up and "Python" not in down  # 100% и там, и там

    # Зарплаты: 28 дней до конца недели (неделя + 3 прошлые; у старых зарплаты нет).
    middle = next(s for s in f.salaries if s.grade == "middle")
    assert middle.count == 50 and middle.previous_count == 0 and middle.previous_median is None
    assert middle.median == 204_500  # 20 по 150k + 30 от 200k с шагом 1k: среднее 25-го и 26-го

    assert [(c.name, c.count, c.previous) for c in f.top_companies] == [("NewCo", 27, 0), ("ООО Ромашка", 3, 20)]
    assert [(c.name, c.count) for c in f.new_companies] == [("NewCo", 27)]


def test_facts_small_week_has_no_technology_moves(pg_db):
    _add([_vac(WEEK) for _ in range(5)])
    with session_scope() as s:
        f = svc.compute_facts(s, WEEK)
    assert f.vacancies == 5 and f.technologies_up == [] and f.technologies_down == []
    assert f.salaries == [] and f.with_salary_share == 0.0


def test_week_helpers():
    assert svc.last_complete_week_start(date(2026, 10, 27)) == date(2026, 10, 19)  # вторник → прошлый пн
    assert svc.last_complete_week_start(date(2026, 10, 26)) == date(2026, 10, 19)  # понедельник
    with pytest.raises(svc.DigestWeekError, match="понедельника"):
        svc.check_week_start(date(2026, 10, 13), today=date(2026, 10, 27))
    with pytest.raises(svc.DigestWeekError, match="не закончилась"):
        svc.check_week_start(date(2026, 10, 26), today=date(2026, 10, 27))
    svc.check_week_start(date(2026, 10, 19), today=date(2026, 10, 26))


# --- LLM-текст


def test_prompt_contains_facts_and_forbids_inventing():
    msgs = build_market_digest_messages(facts_json='{"vacancies": 30}')
    assert '{"vacancies": 30}' in msgs[1]["content"]
    assert "Never invent" in msgs[0]["content"]


def test_market_digest_uses_cover_letter_model_plain_text(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = OpenRouterClient(AiConfig(model="parse/model", cover_letter_model="writer/model"))
    seen: dict = {}

    def _fake_call(*, messages, parse, model=None, plain_text=False, **_kw):
        seen.update(model=model, plain_text=plain_text)
        return parse("```\nТекст обзора\n```"), LLMUsage(model=model or "")

    monkeypatch.setattr(client, "_call_llm", _fake_call)
    text, _ = client.market_digest(facts_json="{}")
    assert text == "Текст обзора"
    assert seen == {"model": "writer/model", "plain_text": True}


@pytest.fixture
def digest_on(monkeypatch, tmp_path):
    use_ai_config(monkeypatch, tmp_path, cover_letter_model="writer/a")
    # use_ai_config кладёт базовый конфиг тестов, где обзор выключен, — включаем.
    import yaml

    from backend.settings import get_settings

    path = get_settings().config_path
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw["digest"] = {"enabled": True, "llm_summary": True}
    Path(path).write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    get_settings.cache_clear()


@pytest.fixture
def fake_summary(monkeypatch):
    calls: list = []

    def _summ(cfg, facts):
        calls.append(facts)
        return f"За неделю {facts.vacancies} вакансий.", "fallback/b"

    monkeypatch.setattr(svc, "_summarize", _summ)
    return calls


def test_generate_stores_and_reuses(pg_db, digest_on, fake_summary):
    _add([_vac(WEEK) for _ in range(3)])
    out = svc.generate_digest(WEEK)
    assert out.summary == "За неделю 3 вакансий." and out.model == "fallback/b"
    assert out.facts.vacancies == 3 and out.stale is False

    assert svc.generate_digest(WEEK).summary == out.summary
    assert len(fake_summary) == 1  # уже есть — без LLM

    _add([_vac(WEEK)])
    assert svc.generate_digest(WEEK, force=True).facts.vacancies == 4
    assert len(fake_summary) == 2
    with session_scope() as s:
        assert s.query(MarketDigestRow).count() == 1

        # Сменилась версия промпта — текст устарел.
        s.execute(update(MarketDigestRow).values(prompt_version="market-digest-v0"))
    with session_scope() as s:
        assert svc.to_out(svc.get_digest(s, WEEK)).stale is True


def test_generate_keeps_facts_when_llm_fails(pg_db, digest_on, monkeypatch):
    def _boom(cfg, facts):
        raise RuntimeError("provider said: secret details")

    monkeypatch.setattr(svc, "_summarize", _boom)
    _add([_vac(WEEK)])
    out = svc.generate_digest(WEEK)
    assert out.summary is None and out.facts.vacancies == 1
    assert out.summary_error == svc.SUMMARY_UNAVAILABLE  # без текста ошибки провайдера


def test_generate_without_llm_summary_and_for_empty_week(pg_db, fake_summary):
    # Базовый конфиг тестов: llm_summary по умолчанию true, но неделя пуста — LLM не зовём.
    out = svc.generate_digest(WEEK)
    assert out.facts.vacancies == 0 and out.summary is None and out.summary_error is None
    assert fake_summary == []


def test_ensure_latest_digest(pg_db, digest_on, fake_summary):
    today = date(2026, 10, 21)
    last = svc.last_complete_week_start(today)
    assert svc.ensure_latest_digest(today=today) is False  # в неделе нет вакансий

    _add([_vac(last + timedelta(days=2))])
    assert svc.ensure_latest_digest(today=today) is True
    assert svc.ensure_latest_digest(today=today) is False  # уже есть
    assert len(fake_summary) == 1


def test_ensure_latest_digest_disabled(pg_db, fake_summary):
    _add([_vac(svc.last_complete_week_start(date(2026, 10, 21)))])
    assert svc.ensure_latest_digest(today=date(2026, 10, 21)) is False
    assert fake_summary == []


# --- API


def test_api_read_for_users_generate_for_admin(api_client, digest_on, fake_summary):
    from tests.conftest import login_as_admin

    admin = login_as_admin(api_client)
    api_client.headers.pop("Authorization", None)
    user = api_client.post("/api/v1/auth/register", json={"email": "u@example.com", "password": "secret1"}).json()[
        "access_token"
    ]
    h_admin, h_user = {"Authorization": f"Bearer {admin}"}, {"Authorization": f"Bearer {user}"}
    last = svc.last_complete_week_start(datetime.now(UTC).date())
    _add([_vac(last + timedelta(days=1)) for _ in range(2)])

    assert api_client.post("/api/v1/digests", headers=h_user, json={}).status_code == 403
    r = api_client.post("/api/v1/digests", headers=h_admin, json={})
    assert r.status_code == 200, r.text
    assert r.json()["period_start"] == last.isoformat() and r.json()["facts"]["vacancies"] == 2

    bad = api_client.post(
        "/api/v1/digests", headers=h_admin, json={"week_start": (last + timedelta(days=1)).isoformat()}
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "VALIDATION"
    future = api_client.post(
        "/api/v1/digests", headers=h_admin, json={"week_start": (last + timedelta(days=7)).isoformat()}
    )
    assert future.status_code == 422

    items = api_client.get("/api/v1/digests", headers=h_user).json()["items"]
    assert [(i["period_start"], i["vacancies"], i["has_summary"]) for i in items] == [(last.isoformat(), 2, True)]
    got = api_client.get(f"/api/v1/digests/{last.isoformat()}", headers=h_user)
    assert got.status_code == 200 and got.json()["summary"].startswith("За неделю")
    assert api_client.get("/api/v1/digests/2020-01-06", headers=h_user).status_code == 404
    assert api_client.get("/api/v1/digests", headers={}).status_code == 401


def test_prompt_v2_rules_new_companies_and_grade_mix():
    system = build_market_digest_messages(facts_json="{}")[0]["content"]
    assert "ONLY those listed in new_companies" in system
    assert "grade mix" in system


def test_prompt_v3_explains_volume_baseline_and_sources():
    system = build_market_digest_messages(facts_json="{}")[0]["content"]
    assert "avg_4w_vacancies" in system
    assert "with_salary_share" in system
    assert "sources" in system


def test_small_salary_group_has_no_median(pg_db):
    rows = [
        _vac(WEEK, detected_grade="lead", detected_salary_avg_rub=300_000.0) for _ in range(svc.MIN_SALARY_GROUP - 1)
    ]
    _add(rows)
    with session_scope() as s:
        lead = next(x for x in svc.compute_facts(s, WEEK).salaries if x.grade == "lead")
    assert lead.count == svc.MIN_SALARY_GROUP - 1 and lead.median is None
