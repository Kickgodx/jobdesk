"""Внутренний API ядра (platform/internal-services): токен, чтение и запись вакансий HH."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import login_as_admin

from backend.db.models import VacancyRow
from backend.db.session import session_scope
from backend.settings import get_settings
from jobdesk.contracts.core_internal import HH_VACANCIES_PATH, MAX_BATCH, VACANCIES_LOOKUP_PATH
from jobdesk.hh.mapper import hh_source_post_key, stable_vacancy_id
from jobdesk.models.vacancy import Vacancy


def _hh_vacancy(hh_id: str, *, text: str = "Python developer", **extra) -> Vacancy:
    return Vacancy(
        id=stable_vacancy_id(hh_id),
        source_post_key=hh_source_post_key(hh_id),
        source_channel="hh",
        source_url="https://hh.ru",
        post_url=f"https://hh.ru/vacancy/{hh_id}",
        post_date=datetime.now(UTC) - timedelta(days=1),
        raw_text=text,
        cleaned_text=text,
        vacancy_score=100,
        **extra,
    )


def _service(client):
    """Заголовки сервиса: токен берётся из настроек (dev-значение или из .env)."""
    return {"Authorization": f"Bearer {get_settings().core_internal_token}"}


def _payload(*vacancies: Vacancy) -> dict:
    return {"vacancies": [v.model_dump(mode="json") for v in vacancies]}


def _count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(VacancyRow)) or 0)


# --- токен -------------------------------------------------------------------------------


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token", "Bearer tgj_personal_token"])
def test_internal_api_requires_service_token(api_client, auth):
    headers = {"Authorization": auth} if auth else {}
    resp = api_client.post(VACANCIES_LOOKUP_PATH, json={"source_post_keys": ["hh-1"]}, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "NOT_AUTHENTICATED"


def test_admin_jwt_cannot_write_vacancies(api_client):
    token = login_as_admin(api_client)
    resp = api_client.post(
        HH_VACANCIES_PATH, json=_payload(_hh_vacancy("1")), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert _count() == 0


def test_internal_api_not_under_public_prefix(api_client):
    resp = api_client.post(
        f"/api/v1{VACANCIES_LOOKUP_PATH}", json={"source_post_keys": ["hh-1"]}, headers=_service(api_client)
    )
    assert resp.status_code == 404


# --- lookup ------------------------------------------------------------------------------


def test_lookup_returns_only_known_keys(api_client):
    assert (
        api_client.post(HH_VACANCIES_PATH, json=_payload(_hh_vacancy("1")), headers=_service(api_client)).status_code
        == 200
    )
    resp = api_client.post(
        VACANCIES_LOOKUP_PATH,
        json={"source_post_keys": [hh_source_post_key("1"), hh_source_post_key("2")]},
        headers=_service(api_client),
    )
    assert resp.status_code == 200
    found = resp.json()["vacancies"]
    assert [v["source_post_key"] for v in found] == [hh_source_post_key("1")]
    assert Vacancy.model_validate(found[0]).raw_text == "Python developer"


def test_lookup_includes_archived_and_duplicates(api_client):
    dup = _hh_vacancy(
        "2", is_duplicate=True, duplicate_group_id=stable_vacancy_id("1"), duplicate_reason="hh_multicity"
    )
    api_client.post(HH_VACANCIES_PATH, json=_payload(_hh_vacancy("1"), dup), headers=_service(api_client))
    with session_scope() as s:
        s.get(VacancyRow, stable_vacancy_id("1")).archived_at = datetime.now(UTC)
    resp = api_client.post(
        VACANCIES_LOOKUP_PATH,
        json={"source_post_keys": [hh_source_post_key("1"), hh_source_post_key("2")]},
        headers=_service(api_client),
    )
    by_key = {v["source_post_key"]: v for v in resp.json()["vacancies"]}
    assert set(by_key) == {hh_source_post_key("1"), hh_source_post_key("2")}
    assert by_key[hh_source_post_key("2")]["is_duplicate"] is True


@pytest.mark.parametrize("n", [0, MAX_BATCH + 1])
def test_lookup_rejects_empty_or_oversized(api_client, n):
    keys = [f"hh-{i}" for i in range(n)]
    resp = api_client.post(VACANCIES_LOOKUP_PATH, json={"source_post_keys": keys}, headers=_service(api_client))
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "VALIDATION"


# --- запись пачки HH ---------------------------------------------------------------------


def test_upsert_reports_inserted_and_updated(api_client):
    api_client.post(HH_VACANCIES_PATH, json=_payload(_hh_vacancy("1")), headers=_service(api_client))
    resp = api_client.post(
        HH_VACANCIES_PATH,
        json=_payload(_hh_vacancy("1", text="Python developer, remote"), _hh_vacancy("2")),
        headers=_service(api_client),
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 1, "updated_existing": 1}
    with session_scope() as s:
        assert s.get(VacancyRow, stable_vacancy_id("1")).raw_text == "Python developer, remote"


def test_repeated_batch_does_not_duplicate(api_client):
    body = _payload(_hh_vacancy("1"), _hh_vacancy("2"))
    assert api_client.post(HH_VACANCIES_PATH, json=body, headers=_service(api_client)).json()["inserted"] == 2
    again = api_client.post(HH_VACANCIES_PATH, json=body, headers=_service(api_client)).json()
    assert again == {"inserted": 0, "updated_existing": 2}
    assert _count() == 2


def test_invalid_vacancy_rejects_whole_batch(api_client):
    body = _payload(_hh_vacancy("1"), _hh_vacancy("2"))
    del body["vacancies"][1]["raw_text"]
    resp = api_client.post(HH_VACANCIES_PATH, json=body, headers=_service(api_client))
    assert resp.status_code == 422
    assert _count() == 0


def test_upsert_rejects_oversized_batch(api_client):
    body = _payload(*[_hh_vacancy(str(i)) for i in range(MAX_BATCH + 1)])
    assert api_client.post(HH_VACANCIES_PATH, json=body, headers=_service(api_client)).status_code == 422
    assert _count() == 0
