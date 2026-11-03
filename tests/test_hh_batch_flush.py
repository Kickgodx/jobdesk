"""Запись вакансий HH пачками через ядро, чтение сохранённых — окнами групп."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tests.hh_support import FakeCore

from jobdesk.config.schema import AppConfig, HhConfig, HhSearchConfig
from jobdesk.hh.dedup import DedupedVacancy
from jobdesk.hh.models import VacancyDetail, VacancyShort
from jobdesk_hh import sync as hh_sync_service
from jobdesk_hh.settings import HhSettings
from jobdesk_platform.service_client import UpstreamUnavailable


def _shorts(n: int) -> list[VacancyShort]:
    return [
        VacancyShort.model_validate(
            {
                "id": str(i),
                "name": f"Job {i}",
                "alternate_url": f"https://hh.ru/vacancy/{i}",
                "employer": {"id": str(i), "name": f"Co{i}"},
                "area": {"name": "Москва"},
                "snippet": {"requirement": f"req {i}", "responsibility": "do"},
                "work_format": [{"id": "REMOTE"}],
                "experience": {"id": "between1And3"},
            }
        )
        for i in range(1, n + 1)
    ]


def _fake_hh_client(shorts: list[VacancyShort]):
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
            return VacancyDetail.model_validate(
                {
                    "id": vacancy_id,
                    "name": f"Job {vacancy_id}",
                    "description": f"<p>desc {vacancy_id}</p>",
                    "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
                    "employer": {"id": vacancy_id, "name": f"Co{vacancy_id}"},
                    "area": {"name": "Москва"},
                    "work_format": [{"id": "REMOTE"}],
                }
            )

    return FakeClient


@pytest.fixture
def marked(monkeypatch) -> list[str]:
    """Listings без БД; возвращает hh id, отмеченные «детали скачаны», по порядку."""
    calls: list[str] = []
    monkeypatch.setattr(hh_sync_service.listings, "upsert_listings_from_shorts", lambda session, shorts: len(shorts))
    monkeypatch.setattr(hh_sync_service.listings, "listing_detail_fetched", lambda *a, **k: False)
    monkeypatch.setattr(hh_sync_service.listings, "mark_detail_fetched", lambda session, hh_id: calls.append(hh_id))
    monkeypatch.setattr(hh_sync_service.listings, "purge_expired_listings", lambda *a, **k: 0)
    return calls


def _run(monkeypatch, shorts, core, *, batch_size: int = 2):
    monkeypatch.setattr(hh_sync_service, "HHClient", _fake_hh_client(shorts))
    config = AppConfig(
        hh=HhConfig(
            enabled=True,
            db_flush_batch_size=batch_size,
            search=HhSearchConfig(text="Python", professional_role=["124"], per_page=100),
        )
    )
    settings = HhSettings(HH_API_TOKEN="t", HH_CONTACT_EMAIL="a@b.c")
    session = MagicMock()
    return hh_sync_service.run_hh_sync(session, config=config, settings=settings, core=core), session


def test_hh_sync_flushes_in_batches(monkeypatch, marked):
    core = FakeCore()
    result, session = _run(monkeypatch, _shorts(5), core)

    assert result.searched == 5
    assert result.details_fetched == 5
    assert result.flush_batches == 3  # 2+2+1
    assert core.flushes == [2, 2, 1]
    assert result.vacancies_inserted == 5
    assert session.commit.call_count >= 3
    assert sorted(marked) == ["1", "2", "3", "4", "5"]


def test_saved_vacancies_are_read_once_per_window(monkeypatch, marked):
    core = FakeCore()
    _run(monkeypatch, _shorts(5), core)
    # Пять групп по одному ключу — одно окно, один lookup на все.
    assert len(core.lookups) == 1
    assert sorted(core.lookups[0]) == sorted(f"hh-{i}" for i in range(1, 6))


def test_details_marked_only_after_core_confirms_batch(monkeypatch, marked):
    def fail_second(flush_no: int) -> None:
        if flush_no == 2:
            raise UpstreamUnavailable("core: соединение не установлено")

    core = FakeCore(on_upsert=fail_second)
    with pytest.raises(UpstreamUnavailable):
        _run(monkeypatch, _shorts(5), core)

    assert core.flushes == [2]  # первая пачка записана
    assert marked == ["1", "2"]  # детали неподтверждённой пачки не отмечены


def _group(gid: str, dups: int) -> DedupedVacancy:
    return DedupedVacancy(
        id=gid, name="n", employer="e", area="a", url="u", duplicates=[(f"{gid}-{i}", None) for i in range(dups)]
    )


def test_lookup_windows_respect_key_limit():
    groups = [_group("a", 1), _group("b", 2), _group("c", 0), _group("d", 5)]
    # ключей: a=2, b=3, c=1, d=6
    assert hh_sync_service.lookup_windows(groups, max_keys=6) == [(0, 3), (3, 4)]
    assert hh_sync_service.lookup_windows(groups, max_keys=3) == [(0, 1), (1, 2), (2, 3), (3, 4)]
    assert hh_sync_service.lookup_windows([], max_keys=3) == []
