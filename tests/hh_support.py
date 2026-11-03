"""Общее для тестов HH-сервиса: подмена внутреннего API ядра без HTTP."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from jobdesk.contracts.core_internal import VacancyUpsertResult
from jobdesk.models.vacancy import Vacancy


class FakeCore:
    """``CoreClient`` в памяти: ``saved`` — вакансии ядра по ``source_post_key``."""

    def __init__(
        self,
        saved: dict[str, Vacancy] | None = None,
        *,
        on_upsert: Callable[[int], None] | None = None,
    ) -> None:
        self.saved = dict(saved or {})
        self.lookups: list[list[str]] = []
        self.flushes: list[int] = []
        self._on_upsert = on_upsert

    def lookup(self, source_post_keys: Sequence[str]) -> dict[str, Vacancy]:
        self.lookups.append(list(source_post_keys))
        return {k: v for k, v in self.saved.items() if k in set(source_post_keys)}

    def upsert_hh(self, vacancies: Sequence[Vacancy]) -> VacancyUpsertResult:
        if self._on_upsert is not None:
            self._on_upsert(len(self.flushes) + 1)
        self.flushes.append(len(vacancies))
        inserted = 0
        for v in vacancies:
            inserted += v.source_post_key not in self.saved
            self.saved[v.source_post_key] = v
        return VacancyUpsertResult(inserted=inserted, updated_existing=len(vacancies) - inserted)

    def close(self) -> None:
        pass
