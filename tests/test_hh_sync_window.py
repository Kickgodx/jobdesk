import pytest

from jobdesk_hh.window import HhSyncWindowError, resolve_hh_search_window


def test_default_and_all():
    r = resolve_hh_search_window()
    assert r.window == "all" and r.period is None and r.date_from is None
    r2 = resolve_hh_search_window(window="all")
    assert r2.window == "all" and r2.period is None


@pytest.mark.parametrize("window,period", [("7d", 7), ("14d", 14), ("30d", 30)])
def test_presets_use_period(window, period):
    r = resolve_hh_search_window(window=window, date_from="2026-01-01", date_to="2026-01-31")
    assert r.period == period
    assert r.date_from is None and r.date_to is None


def test_range_ok():
    r = resolve_hh_search_window(window="range", date_from="2026-10-01", date_to="2026-10-10")
    assert r.period is None
    assert r.date_from == "2026-10-01" and r.date_to == "2026-10-10"


def test_range_requires_both_dates():
    with pytest.raises(HhSyncWindowError):
        resolve_hh_search_window(window="range", date_from="2026-10-01")
    with pytest.raises(HhSyncWindowError):
        resolve_hh_search_window(window="range", date_to="2026-10-10")


def test_range_from_after_to():
    with pytest.raises(HhSyncWindowError):
        resolve_hh_search_window(window="range", date_from="2026-10-10", date_to="2026-10-01")


def test_unknown_window():
    with pytest.raises(HhSyncWindowError):
        resolve_hh_search_window(window="week")
