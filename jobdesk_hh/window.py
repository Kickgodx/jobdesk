"""Окно поиска HH sync: all | 7d | 14d | 30d | range (``sources/hh-sync``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

HhSyncWindow = Literal["all", "7d", "14d", "30d", "range"]

_PRESET_PERIODS: dict[str, int] = {"7d": 7, "14d": 14, "30d": 30}
_ALLOWED = frozenset({"all", "7d", "14d", "30d", "range"})


class HhSyncWindowError(ValueError):
    """Невалидное окно sync — API отдаёт 422."""


@dataclass(frozen=True)
class ResolvedHhSearchWindow:
    window: HhSyncWindow
    period: int | None = None
    date_from: str | None = None
    date_to: str | None = None


def resolve_hh_search_window(
    *,
    window: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ResolvedHhSearchWindow:
    w = (window or "all").strip().lower() or "all"
    if w not in _ALLOWED:
        raise HhSyncWindowError(f"Unknown HH sync window: {window!r}")
    if w == "all":
        return ResolvedHhSearchWindow(window="all")
    if w in _PRESET_PERIODS:
        return ResolvedHhSearchWindow(window=w, period=_PRESET_PERIODS[w])  # type: ignore[arg-type]
    # range
    df = (date_from or "").strip()
    dt = (date_to or "").strip()
    if not df or not dt:
        raise HhSyncWindowError("window=range requires date_from and date_to (YYYY-MM-DD)")
    try:
        d_from = date.fromisoformat(df)
        d_to = date.fromisoformat(dt)
    except ValueError as exc:
        raise HhSyncWindowError("date_from/date_to must be YYYY-MM-DD") from exc
    if d_from > d_to:
        raise HhSyncWindowError("date_from must be <= date_to")
    return ResolvedHhSearchWindow(window="range", date_from=df, date_to=dt)
