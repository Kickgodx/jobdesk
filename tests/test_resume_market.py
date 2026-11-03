"""Резюме против рынка: ближайшие к резюме вакансии — вилка, профиль, технологии в резюме."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from tests.llm_job_helpers import use_ai_config

from backend.db.models import VacancyRow
from backend.db.session import session_scope
from backend.db.vector_type import EMBEDDING_DIMENSIONS
from backend.services import embedding_service
from backend.services.resume_market_service import (
    DEFAULT_PEERS,
    MAX_PEERS,
    MIN_PEERS,
    MIN_SALARY_PEERS,
    SALARY_PEERS,
    WINDOW_DAYS,
    quantile,
)

_TOPICS = ["python", "golang", "qa", "тестиров"]


def _fake_vector(text: str) -> list[float]:
    t = text.lower()
    v = [0.0] * EMBEDDING_DIMENSIONS
    for i, topic in enumerate(_TOPICS):
        v[i] = float(t.count(topic))
    v[len(_TOPICS)] = 0.05
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


@pytest.fixture
def fake_embed(monkeypatch, tmp_path):
    use_ai_config(monkeypatch, tmp_path, embeddings={"enabled": True})
    embedding_service.reset_embedding_client()
    monkeypatch.setattr(embedding_service, "embed_texts", lambda texts, *, ai=None: [_fake_vector(t) for t in texts])
    yield
    embedding_service.reset_embedding_client()


_n = iter(range(1, 1_000_000))


def _vac(text: str, *, days_ago=1, duplicate=False, archived=False, **kw) -> VacancyRow:
    i = next(_n)
    now = datetime.now(UTC)
    return VacancyRow(
        id=f"rm-{i}",
        source_post_key=f"k-{i}",
        source_channel="ch",
        source_url="u",
        post_date=now - timedelta(days=days_ago),
        raw_text=text,
        cleaned_text=text,
        is_duplicate=duplicate,
        archived_at=now if archived else None,
        **kw,
    )


def _add(*rows) -> None:
    with session_scope() as s:
        s.add_all(rows)
    embedding_service.sweep_vacancies()


def _token(client, email="a@example.com") -> str:
    return client.post("/api/v1/auth/register", json={"email": email, "password": "secret1"}).json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _resume(client, token, text) -> int:
    return client.post("/api/v1/resumes", headers=_h(token), json={"name": "CV", "text": text}).json()["id"]


def test_quantile_matches_percentile_cont():
    assert quantile([10.0], 0.9) == 10.0
    assert quantile([100.0, 200.0, 300.0, 400.0], 0.5) == 250.0
    assert quantile([100.0, 200.0, 300.0, 400.0], 0.25) == 175.0


def test_market_profile_salary_and_technologies(api_client, fake_embed):
    qa = [
        _vac(
            "QA тестировщик python",
            detected_grade="middle" if i < 7 else "senior",
            detected_specializations=["Automation QA"],
            technologies=["Python", "Pytest"] if i % 2 else ["Python", "Selenium"],
            detected_salary_avg_rub=100_000 + 10_000 * i,
            archived=i == 0,  # архив — тоже рынок
        )
        for i in range(10)
    ]
    go = [
        _vac(
            "golang golang golang",
            detected_grade="senior",
            detected_specializations=["Backend"],
            technologies=["Go"],
            detected_salary_avg_rub=900_000,
        )
        for _ in range(45)
    ]
    excluded = [
        _vac("QA тестировщик python", duplicate=True, detected_salary_avg_rub=5_000_000),
        _vac("QA тестировщик python", days_ago=WINDOW_DAYS + 5, detected_salary_avg_rub=5_000_000),
    ]
    _add(*qa, *go, *excluded)
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик: Python, pytest, немного Docker")

    r = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token), params={"peers": 50})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["peers"] == 50  # 10 QA ближе всех, дальше Go; дубль и старая — мимо
    assert m["grade"] is None
    assert m["specializations"][0] == {"name": "Backend", "share": 0.8}
    assert {"name": "Automation QA", "share": 0.2} in m["specializations"]
    techs = {t["name"]: t for t in m["technologies"]}
    assert techs["Python"]["in_resume"] and techs["Pytest"]["in_resume"]
    assert not techs["Selenium"]["in_resume"] and not techs["Go"]["in_resume"]
    assert techs["Go"]["share"] == 0.8
    assert {"Python", "Pytest"} <= set(m["resume_technologies"])
    assert m["grades"][0]["name"] == "senior"
    # Зарплата — по всем ближайшим с зарплатой той же выборки: 10 QA + 40 Go.
    assert m["salary_count"] == 50
    assert m["p10"] == pytest.approx(quantile(sorted([100_000 + 10_000 * i for i in range(10)] + [900_000] * 40), 0.1))
    assert m["median"] == 900_000
    # Python+Pytest — весь стек в резюме, Python+Selenium — половина, Go — ничего.
    assert 0 < m["coverage"] < 1


def test_market_without_enough_salaries(api_client, fake_embed):
    _add(*[_vac("QA тестировщик", detected_salary_avg_rub=100_000) for _ in range(MIN_SALARY_PEERS - 1)])
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик")
    m = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token)).json()
    assert m["peers"] == MIN_SALARY_PEERS - 1
    assert m["salary_count"] == 0 and m["median"] is None
    assert m["technologies"] == [] and m["coverage"] is None


def test_market_empty_catalog_and_access(api_client, fake_embed):
    token = _token(api_client)
    rid = _resume(api_client, token, "QA")
    m = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token)).json()
    assert m["peers"] == 0 and m["grades"] == []

    other = _token(api_client, "b@example.com")
    assert api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(other)).status_code == 404


def test_market_embeddings_disabled_is_503(api_client):
    token = _token(api_client)
    rid = _resume(api_client, token, "QA")
    r = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "UPSTREAM_UNAVAILABLE"


def test_nested_technology_name_counts_as_in_resume(api_client, fake_embed):
    """«Atlassian Jira» в резюме — это Jira вакансии; словарь видит только длинное имя."""
    _add(*[_vac("QA тестировщик", technologies=["Jira", "Atlassian Jira" if i < 5 else "Jira"]) for i in range(10)])
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик, трекер Atlassian Jira")
    m = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token)).json()
    techs = {t["name"]: t["in_resume"] for t in m["technologies"]}
    assert techs["Jira"] is True and techs["Atlassian Jira"] is True
    assert m["coverage"] == 1.0


def test_market_peers_default_and_bounds(api_client, fake_embed):
    _add(*[_vac("QA тестировщик python", detected_grade="middle") for _ in range(120)])
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик")
    url = f"/api/v1/resumes/{rid}/market"
    assert api_client.get(url, headers=_h(token)).json()["peers"] == DEFAULT_PEERS
    assert api_client.get(url, headers=_h(token), params={"peers": 20}).json()["peers"] == 20
    assert api_client.get(url, headers=_h(token), params={"peers": MAX_PEERS}).json()["peers"] == 120
    assert api_client.get(url, headers=_h(token), params={"peers": MAX_PEERS + 1}).status_code == 422
    assert api_client.get(url, headers=_h(token), params={"peers": MIN_PEERS - 1}).status_code == 422


def test_market_grade_filter(api_client, fake_embed):
    """Фильтр по грейду берёт ближайших этого грейда, даже если другие грейды ближе."""
    near_middle = [
        _vac("QA тестировщик python", detected_grade="middle", detected_salary_avg_rub=150_000) for _ in range(30)
    ]
    far_senior = [
        _vac(
            "QA тестировщик python golang",
            detected_grade="senior",
            technologies=["Kubernetes"],
            detected_salary_avg_rub=350_000,
        )
        for _ in range(10)
    ]
    _add(*near_middle, *far_senior)
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик python")
    url = f"/api/v1/resumes/{rid}/market"

    m = api_client.get(url, headers=_h(token), params={"peers": 20}).json()
    assert m["grades"] == [{"name": "middle", "share": 1.0}]

    m = api_client.get(url, headers=_h(token), params={"peers": 20, "grade": "Senior"}).json()
    assert m["grade"] == "senior"
    assert m["peers"] == 10
    assert m["grades"] == [{"name": "senior", "share": 1.0}]
    assert m["technologies"][0]["name"] == "Kubernetes"
    assert m["salary_count"] == 10 and m["median"] == 350_000

    r = api_client.get(url, headers=_h(token), params={"grade": "boss"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION"


def test_market_salary_extends_past_small_sample(api_client, fake_embed):
    """В малой выборке мало зарплат — добираем ближайших с зарплатой из кандидатов."""
    no_salary = [_vac("QA тестировщик python") for _ in range(15)]
    paid = [_vac("QA тестировщик python golang", detected_salary_avg_rub=200_000) for _ in range(40)]
    _add(*no_salary, *paid)
    token = _token(api_client)
    rid = _resume(api_client, token, "QA тестировщик python")
    m = api_client.get(f"/api/v1/resumes/{rid}/market", headers=_h(token), params={"peers": 20}).json()
    assert m["peers"] == 20
    assert m["salary_count"] == SALARY_PEERS
