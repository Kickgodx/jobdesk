"""Регрессии по Bugbot: content_hash reset, канон/дубли, mid-group flush."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from tests.hh_support import FakeCore

from jobdesk.config.schema import AppConfig, HhConfig, HhSearchConfig
from jobdesk.hh.client import HHApiError
from jobdesk.hh.dedup import DedupedVacancy
from jobdesk.hh.mapper import hh_source_post_key, stable_vacancy_id
from jobdesk.hh.models import VacancyShort
from jobdesk.models.vacancy import Vacancy
from jobdesk_hh import sync as hh_sync_service
from jobdesk_hh.db import listings as hh_repo
from jobdesk_hh.db.models import HhListingRow
from jobdesk_hh.settings import HhSettings


def _short(vid: str, *, area: str = "Москва", req: str = "Python") -> VacancyShort:
    return VacancyShort.model_validate(
        {
            "id": vid,
            "name": "QA Automation Engineer",
            "alternate_url": f"https://hh.ru/vacancy/{vid}",
            "employer": {"id": "100", "name": "ACME"},
            "area": {"name": area},
            "snippet": {"requirement": req, "responsibility": "автотесты"},
            "work_format": [{"id": "REMOTE"}],
            "experience": {"id": "between1And3"},
        }
    )


def test_content_hash_change_resets_detail_fetched_at(db_session):
    now = datetime.now(UTC)

    with db_session as session:
        session.add(
            HhListingRow(
                hh_id="1",
                name="QA",
                content_hash="oldhash",
                detail_fetched_at=now,
                last_seen_at=now,
            )
        )
        session.commit()

        hh_repo.upsert_listings_from_shorts(session, [_short("1", req="Java")])
        session.commit()
        row = session.get(HhListingRow, "1")
        assert row is not None
        assert row.detail_fetched_at is None
        assert row.content_hash != "oldhash"


def test_employer_area_truncated(db_session):
    long_name = "X" * 400
    short = VacancyShort.model_validate(
        {
            "id": "9",
            "name": "Job",
            "employer": {"name": long_name},
            "area": {"name": long_name},
        }
    )
    with db_session as session:
        hh_repo.upsert_listings_from_shorts(session, [short])
        session.commit()
        row = session.get(HhListingRow, "9")
        assert row is not None
        assert len(row.employer or "") <= 256
        assert len(row.area or "") <= 256


def test_stabilize_prefers_detail_fetched_canonical(db_session):
    now = datetime.now(UTC)
    by_id = {
        "1": _short("1", area="Москва"),
        "2": _short("2", area="Казань"),
    }
    groups = [
        DedupedVacancy(
            id="1",
            name="QA",
            employer="ACME",
            area="Москва",
            url="https://hh.ru/vacancy/1",
            duplicates=[("2", "Казань")],
        )
    ]
    with db_session as session:
        session.add(
            HhListingRow(
                hh_id="2",
                name="QA",
                content_hash="h",
                detail_fetched_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
        stabilized = hh_sync_service._stabilize_groups(session, groups, by_id)
        assert len(stabilized) == 1
        assert stabilized[0].id == "2"
        assert stabilized[0].duplicates == [("1", "Москва")]


def _saved_detail_vacancy(vid: str) -> dict[str, Vacancy]:
    """Сохранённая в ядре карточка с деталями — так её отдаёт lookup внутреннего API."""
    v = Vacancy(
        id=stable_vacancy_id(vid),
        source_post_key=hh_source_post_key(vid),
        source_channel="hh",
        source_url="https://hh.ru",
        post_url=f"https://hh.ru/vacancy/{vid}",
        raw_text="<p>full detail body</p>",
        cleaned_text="full detail body",
        extraction_source="hh_api",
        vacancy_score=100,
    )
    return {v.source_post_key: v}


def test_duplicate_does_not_overwrite_existing_detail_vacancy(db_session):
    vid = "42"
    existing = _saved_detail_vacancy(vid)
    with db_session as session:
        buffer = hh_sync_service._Buffer()
        result = hh_sync_service.HhSyncResult()
        hh_sync_service._append_duplicate(
            MagicMock(),
            session,
            _short(vid, area="Казань"),
            result,
            buffer,
            existing,
            group_vacancy_id=stable_vacancy_id("1"),
        )
        assert len(buffer.vacancies) == 1
        assert "full detail body" in buffer.vacancies[0].cleaned_text
        assert buffer.vacancies[0].is_duplicate is True
        # Окно lookup общее для групп: сохранённая копия не меняется.
        assert existing[hh_source_post_key(vid)].is_duplicate is False


def test_failed_detail_refetch_keeps_saved_card():
    vid = "77"
    client = MagicMock()
    client.get_vacancy.side_effect = HHApiError("unavailable", status_code=503)
    buffer = hh_sync_service._Buffer()
    result = hh_sync_service.HhSyncResult()

    hh_sync_service._fetch_into_buffer(
        client, _short(vid), result, buffer, _saved_detail_vacancy(vid), is_duplicate=False
    )

    assert len(buffer.vacancies) == 1
    assert buffer.vacancies[0].cleaned_text == "full detail body"
    assert buffer.vacancies[0].is_duplicate is False
    assert buffer.fetched_details == []  # детали не отмечены — следующий sync попробует снова
    assert result.errors == [f"detail {vid}: HTTP 503"]
    assert result.details_fetched == 0


def test_failed_detail_of_new_vacancy_falls_back_to_short():
    client = MagicMock()
    client.get_vacancy.side_effect = HHApiError("unavailable", status_code=503)
    buffer = hh_sync_service._Buffer()
    result = hh_sync_service.HhSyncResult()

    hh_sync_service._fetch_into_buffer(client, _short("78"), result, buffer, {}, is_duplicate=False)

    assert len(buffer.vacancies) == 1
    assert "QA Automation Engineer" in buffer.vacancies[0].cleaned_text
    assert result.errors == ["detail 78: HTTP 503"]


def test_mid_group_flush_when_duplicates_exceed_batch(monkeypatch):
    """Мультигородной кластер > batch_size — flush внутри группы."""
    shorts = [_short(str(i), area=f"City{i}") for i in range(1, 6)]

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def iter_vacancy_pages(self, *a, **k):
            page = MagicMock()
            page.found = len(shorts)
            page.items = shorts
            yield page

        def get_vacancy(self, vacancy_id: str):
            from jobdesk.hh.models import VacancyDetail

            return VacancyDetail.model_validate(
                {
                    "id": vacancy_id,
                    "name": "QA Automation Engineer",
                    "description": f"<p>desc {vacancy_id}</p>",
                    "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
                    "employer": {"id": "100", "name": "ACME"},
                    "area": {"name": "Москва"},
                    "work_format": [{"id": "REMOTE"}],
                    "snippet": {"requirement": "Python", "responsibility": "автотесты"},
                    "experience": {"id": "between1And3"},
                }
            )

    # Все shorts дедупятся в одну группу (одинаковый content key).
    monkeypatch.setattr(hh_sync_service, "HHClient", FakeClient)
    monkeypatch.setattr(hh_sync_service.listings, "upsert_listings_from_shorts", lambda *a, **k: len(shorts))
    monkeypatch.setattr(hh_sync_service.listings, "listing_detail_fetched", lambda *a, **k: False)
    monkeypatch.setattr(hh_sync_service.listings, "mark_detail_fetched", lambda *a, **k: None)
    monkeypatch.setattr(hh_sync_service.listings, "purge_expired_listings", lambda *a, **k: 0)
    core = FakeCore()

    session = MagicMock()
    config = AppConfig(
        hh=HhConfig(
            enabled=True,
            db_flush_batch_size=2,
            search=HhSearchConfig(text="Python", professional_role=["124"], per_page=100),
        )
    )
    settings = HhSettings(HH_API_TOKEN="t", HH_CONTACT_EMAIL="a@b.c")
    result = hh_sync_service.run_hh_sync(session, config=config, settings=settings, core=core)

    assert result.unique == 1
    assert result.duplicates == 4
    # 1 detail + 4 short dups = 5 → flush 2+2+1
    assert core.flushes == [2, 2, 1]
    assert result.flush_batches == 3
