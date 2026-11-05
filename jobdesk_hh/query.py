"""Поисковый запрос HH sync: main | no_python (из ``hh.search``) | custom (текст из запроса)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jobdesk.config.schema import HhSearchConfig

HhSearchQuery = Literal["main", "no_python", "custom"]

_ALLOWED = frozenset({"main", "no_python", "custom"})
CUSTOM_TEXT_MAX_LENGTH = 1000


class HhSearchQueryError(ValueError):
    """Невалидный запрос sync — API отдаёт 422."""


@dataclass(frozen=True)
class ResolvedHhSearchQuery:
    query: HhSearchQuery
    text: str


def resolve_hh_search_query(
    search: HhSearchConfig,
    *,
    query: str | None = None,
    text: str | None = None,
) -> ResolvedHhSearchQuery:
    """Текст запроса фиксируется при постановке sync: повтор после recover ищет то же самое."""
    q = (query or "main").strip().lower() or "main"
    if q not in _ALLOWED:
        raise HhSearchQueryError(f"Unknown HH search query: {query!r}")
    custom = (text or "").strip()
    if q != "custom":
        if custom:
            raise HhSearchQueryError("text is allowed only with query=custom")
        return ResolvedHhSearchQuery(query=q, text=search.text if q == "main" else search.text_no_python)  # type: ignore[arg-type]
    if not custom:
        raise HhSearchQueryError("query=custom requires non-empty text")
    if len(custom) > CUSTOM_TEXT_MAX_LENGTH:
        raise HhSearchQueryError(f"text must be at most {CUSTOM_TEXT_MAX_LENGTH} characters")
    return ResolvedHhSearchQuery(query="custom", text=custom)
