"""Синхронизация вакансий HH: search → listings → dedup → detail (если нужно) → ядро.

Кэш выдачи и lock — в схеме ``hh`` (сессия сервиса); вакансии читаются и пишутся через
внутренний API ядра (``CoreClient``): сохранённые вакансии групп — пачкой на окно групп,
новые и обновлённые — пачками по ``hh.db_flush_batch_size``. Детали listing отмечаются
скачанными только после того, как ядро подтвердило запись пачки: иначе следующий sync
пропустил бы детали вакансии, которой нет в базе.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry import trace
from sqlalchemy.orm import Session

from jobdesk.config.schema import AppConfig, HhConfig
from jobdesk.contracts.core_internal import MAX_BATCH
from jobdesk.hh.client import HH_VACANCIES_DEPTH_LIMIT, HHApiError, HHClient, HHClientConfig
from jobdesk.hh.dedup import DedupedVacancy, dedupe_vacancies
from jobdesk.hh.mapper import (
    detail_to_vacancy,
    hh_source_post_key,
    short_to_vacancy,
    stable_vacancy_id,
)
from jobdesk.hh.models import VacancySearchParams, VacancyShort
from jobdesk.models.vacancy import Vacancy
from jobdesk_hh.core_client import CoreClient
from jobdesk_hh.db import listings
from jobdesk_hh.observability import (
    record_hh_api_error,
    record_hh_details,
    record_hh_sync_finished,
)
from jobdesk_hh.query import ResolvedHhSearchQuery
from jobdesk_hh.settings import HhSettings
from jobdesk_hh.window import ResolvedHhSearchWindow

_LOG = logging.getLogger(__name__)
_TRACER = trace.get_tracer(__name__)
# In-process lock: второй sync в том же процессе отказывает сразу, без похода в БД.
_SYNC_LOCK = threading.Lock()


class HhSyncConflict(RuntimeError):
    """Параллельный sync уже идёт."""


class HhSyncNotConfigured(RuntimeError):
    """HH выключен или нет токена/email."""


# (phase, result, done, total): search → details (done/total групп) → purge.
# Исключение из колбэка прерывает sync (так воркер останавливает его при потере lease).
HhSyncProgressCallback = Callable[[str, "HhSyncResult", int, int], None]


@dataclass
class HhSyncResult:
    searched: int = 0
    listings_upserted: int = 0
    unique: int = 0
    duplicates: int = 0
    details_fetched: int = 0
    details_skipped: int = 0
    vacancies_inserted: int = 0
    vacancies_updated: int = 0
    listings_purged: int = 0
    pages: int = 0
    flush_batches: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    window: str = "all"
    period: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    query: str = "main"
    search_text: str | None = None


@dataclass
class _Buffer:
    """Вакансии к записи в ядро и hh id, чьи детали скачаны для этих вакансий."""

    vacancies: list[Vacancy] = field(default_factory=list)
    fetched_details: list[str] = field(default_factory=list)


def ensure_hh_ready(settings: HhSettings, hh: HhConfig) -> None:
    """Проверить конфиг без открытия HTTP-клиента."""
    token = (settings.hh_api_token or "").strip()
    email = (settings.hh_contact_email or "").strip()
    if not hh.enabled:
        raise HhSyncNotConfigured("HH sync disabled (hh.enabled=false)")
    if not token:
        raise HhSyncNotConfigured("HH_API_TOKEN is not set")
    if not email:
        raise HhSyncNotConfigured("HH_CONTACT_EMAIL is not set")


def _client_from(settings: HhSettings, hh: HhConfig) -> HHClient:
    ensure_hh_ready(settings, hh)
    return HHClient(
        HHClientConfig(
            api_token=(settings.hh_api_token or "").strip(),
            contact_email=(settings.hh_contact_email or "").strip(),
            api_base=hh.api_base,
            host=hh.host,
            app_name=hh.app_name,
            app_version=hh.app_version,
            max_rps=hh.max_rps,
        )
    )


def _group_keys(group: DedupedVacancy) -> list[str]:
    return [hh_source_post_key(group.id), *(hh_source_post_key(dup_id) for dup_id, _ in group.duplicates)]


def lookup_windows(groups: list[DedupedVacancy], *, max_keys: int = MAX_BATCH) -> list[tuple[int, int]]:
    """Разбить группы на окна ``[start, end)``, у которых не больше ``max_keys`` ключей.

    Группа крупнее лимита — окно из одной группы (``CoreClient.lookup`` порежет её сам).
    """
    windows: list[tuple[int, int]] = []
    start, keys = 0, 0
    for i, group in enumerate(groups):
        n = 1 + len(group.duplicates)
        if i > start and keys + n > max_keys:
            windows.append((start, i))
            start, keys = i, 0
        keys += n
    if start < len(groups):
        windows.append((start, len(groups)))
    return windows


def run_hh_sync(
    session: Session,
    *,
    config: AppConfig,
    settings: HhSettings,
    core: CoreClient,
    search_window: ResolvedHhSearchWindow | None = None,
    search_query: ResolvedHhSearchQuery | None = None,
    progress_callback: HhSyncProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> HhSyncResult:
    """Поиск → детали групп → purge listings.

    ``session`` — сессия схемы hh (кэш выдачи); вакансии — через ``core``.
    ``should_stop`` проверяется перед каждой следующей страницей поиска и группой
    деталей: True → sync завершается досрочно — уже полученные вакансии дописываются,
    purge пропускается, возвращается частичный итог (отмену фиксирует вызывающий).
    """
    stop_requested = should_stop or (lambda: False)
    thread_locked = _SYNC_LOCK.acquire(blocking=False)
    if not thread_locked:
        raise HhSyncConflict("HH sync already in progress")
    db_lock = listings.HhSyncDbLock(session)
    started = time.monotonic()
    result = HhSyncResult()
    outcome = "success"
    resolved = search_window or ResolvedHhSearchWindow(window="all")
    result.window = resolved.window
    result.period = resolved.period
    result.date_from = resolved.date_from
    result.date_to = resolved.date_to
    query = search_query or ResolvedHhSearchQuery(query="main", text=config.hh.search.text)
    result.query = query.query
    result.search_text = query.text

    def report(phase: str, done: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback(phase, result, done, total)

    try:
        if not db_lock.try_acquire():
            raise HhSyncConflict("HH sync already in progress")

        with _TRACER.start_as_current_span("hh.sync") as span:
            hh = config.hh
            client = _client_from(settings, hh)
            try:
                report("search")
                span.set_attribute("hh.window", resolved.window)
                span.set_attribute("hh.query", query.query)
                if resolved.period is not None:
                    span.set_attribute("hh.period", resolved.period)
                if resolved.date_from:
                    span.set_attribute("hh.date_from", resolved.date_from)
                if resolved.date_to:
                    span.set_attribute("hh.date_to", resolved.date_to)
                shorts, pages, stopped = _collect_shorts(
                    client,
                    hh,
                    resolved,
                    text=query.text,
                    should_stop=stop_requested,
                    warnings=result.warnings,
                )
                result.pages = pages
                result.searched = len(shorts)
                span.set_attribute("hh.searched", result.searched)
                span.set_attribute("hh.pages", pages)

                with _TRACER.start_as_current_span("hh.listings_upsert"):
                    result.listings_upserted = listings.upsert_listings_from_shorts(session, shorts)
                    session.commit()

                by_id = {s.id: s for s in shorts}
                with _TRACER.start_as_current_span("hh.dedup") as dedup_span:
                    groups = _stabilize_groups(session, dedupe_vacancies(shorts), by_id)
                    result.unique = len(groups)
                    result.duplicates = sum(len(g.duplicates) for g in groups)
                    dedup_span.set_attribute("hh.unique", result.unique)
                    dedup_span.set_attribute("hh.duplicates", result.duplicates)
                shorts.clear()
                report("details", 0, len(groups))

                batch_size = hh.db_flush_batch_size
                buffer = _Buffer()

                def flush_buffer(*, force: bool = False) -> None:
                    if not buffer.vacancies:
                        return
                    if not force and len(buffer.vacancies) < batch_size:
                        return
                    with _TRACER.start_as_current_span("hh.upsert") as upsert_span:
                        stats = core.upsert_hh(buffer.vacancies)
                        result.vacancies_inserted += stats.inserted
                        result.vacancies_updated += stats.updated_existing
                        result.flush_batches += 1
                        upsert_span.set_attribute("hh.batch_size", len(buffer.vacancies))
                        upsert_span.set_attribute("hh.inserted", stats.inserted)
                        upsert_span.set_attribute("hh.updated", stats.updated_existing)
                    # Ядро записало пачку — теперь детали можно считать скачанными.
                    for hh_id in buffer.fetched_details:
                        listings.mark_detail_fetched(session, hh_id)
                    session.commit()
                    _LOG.info(
                        "HH flush batch #%s: %s vacancies (+%s/~%s)",
                        result.flush_batches,
                        len(buffer.vacancies),
                        stats.inserted,
                        stats.updated_existing,
                    )
                    buffer.vacancies.clear()
                    buffer.fetched_details.clear()

                done = 0
                for start, end in lookup_windows(groups):
                    if stopped or stop_requested():
                        stopped = True
                        break
                    window_groups = groups[start:end]
                    with _TRACER.start_as_current_span("hh.lookup") as lookup_span:
                        existing = core.lookup([key for g in window_groups for key in _group_keys(g)])
                        lookup_span.set_attribute("hh.groups", len(window_groups))
                        lookup_span.set_attribute("hh.found", len(existing))
                    for group in window_groups:
                        if stopped or stop_requested():
                            stopped = True
                            break
                        _process_group(
                            client,
                            session,
                            group,
                            by_id,
                            result,
                            buffer,
                            existing,
                            on_buffered=lambda: flush_buffer(force=False),
                        )
                        by_id.pop(group.id, None)
                        for dup_id, _ in group.duplicates:
                            by_id.pop(dup_id, None)
                        flush_buffer()
                        done += 1
                        report("details", done, len(groups))

                # И при отмене: детали в буфере уже оплачены запросами к HH.
                flush_buffer(force=True)
                if not stopped:
                    report("purge", len(groups), len(groups))
                groups.clear()
                by_id.clear()
                if stopped:
                    outcome = "cancelled"
                    _LOG.info("HH sync: остановлен по запросу отмены, purge listings пропущен")
                else:
                    result.listings_purged = listings.purge_expired_listings(session, ttl_days=hh.listing_ttl_days)
                    session.commit()
                span.set_attribute("hh.details_fetched", result.details_fetched)
                span.set_attribute("hh.details_skipped", result.details_skipped)
                span.set_attribute("hh.flush_batches", result.flush_batches)
            finally:
                client.close()
    except HhSyncNotConfigured:
        outcome = "not_configured"
        raise
    except HHApiError as exc:
        outcome = "api_error"
        record_hh_api_error(status=str(exc.status_code or "unknown"))
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        try:
            db_lock.release()
        except Exception as exc:
            _LOG.warning("HH advisory lock release failed: %s", exc)
        if thread_locked:
            _SYNC_LOCK.release()
        result.duration_seconds = time.monotonic() - started
        record_hh_sync_finished(
            outcome=outcome,
            duration_seconds=result.duration_seconds,
            searched=result.searched,
        )
        record_hh_details(fetched=result.details_fetched, skipped=result.details_skipped)
        _LOG.info(
            "HH sync done in %.1fs: query=%s window=%s period=%s date_from=%s date_to=%s "
            "searched=%s unique=%s dup=%s detail_fetch=%s skip=%s vac +%s/~%s "
            "batches=%s errors=%s",
            result.duration_seconds,
            result.query,
            result.window,
            result.period,
            result.date_from,
            result.date_to,
            result.searched,
            result.unique,
            result.duplicates,
            result.details_fetched,
            result.details_skipped,
            result.vacancies_inserted,
            result.vacancies_updated,
            result.flush_batches,
            len(result.errors),
        )
    return result


def _collect_shorts(
    client: HHClient,
    hh: HhConfig,
    search_window: ResolvedHhSearchWindow | None = None,
    *,
    text: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[VacancyShort], int, bool]:
    """Страницы поиска; третий элемент — остановлено ли по ``should_stop`` (следующая страница не запрошена).

    Если HH нашёл больше, чем отдаёт его лимит глубины, в ``warnings`` — предупреждение:
    остаток выдачи иначе терялся бы молча.
    """
    resolved = search_window or ResolvedHhSearchWindow(window="all")
    params = VacancySearchParams(
        text=text or hh.search.text,
        professional_role=list(hh.search.professional_role),
        per_page=hh.search.per_page,
        period=resolved.period,
        date_from=resolved.date_from,
        date_to=resolved.date_to,
    )
    shorts: list[VacancyShort] = []
    pages = 0
    found = 0
    if should_stop is not None and should_stop():
        return shorts, pages, True
    with _TRACER.start_as_current_span("hh.search"):
        for page in client.iter_vacancy_pages(params):
            pages += 1
            found = max(found, int(page.found or 0))
            shorts.extend(page.items)
            # Генератор запрашивает следующую страницу только на следующем шаге цикла.
            if should_stop is not None and should_stop():
                return shorts, pages, True
    if warnings is not None and found > HH_VACANCIES_DEPTH_LIMIT and found > len(shorts):
        warnings.append(
            f"HH нашёл {found} вакансий, прочитано {len(shorts)} (лимит глубины HH — "
            f"{HH_VACANCIES_DEPTH_LIMIT}): сузьте окно sync или поисковый запрос."
        )
    return shorts, pages, False


def _stabilize_groups(
    session: Session,
    groups: list[DedupedVacancy],
    by_id: dict[str, VacancyShort],
) -> list[DedupedVacancy]:
    """
    Канон = участник группы, у которого уже есть detail (если есть).

    Иначе порядок выдачи HH при повторном sync меняет канон, и бывшая
    detail-карточка попадает в duplicates и затирается short-stub'ом.
    """
    out: list[DedupedVacancy] = []
    for group in groups:
        members: list[tuple[str, str | None]] = [(group.id, group.area)]
        members.extend(group.duplicates)
        preferred_id: str | None = None
        for mid, _area in members:
            if listings.listing_detail_fetched(session, mid):
                preferred_id = mid
                break
        if preferred_id is None or preferred_id == group.id:
            out.append(group)
            continue
        short = by_id.get(preferred_id)
        preferred_area = next((a for mid, a in members if mid == preferred_id), None)
        new_dups = [(mid, area) for mid, area in members if mid != preferred_id]
        out.append(
            DedupedVacancy(
                id=preferred_id,
                name=short.name if short else group.name,
                employer=(short.employer.name if short and short.employer else group.employer),
                area=preferred_area,
                url=short.alternate_url if short else group.url,
                duplicates=new_dups,
            )
        )
        _LOG.debug(
            "HH dedup: канон %s → %s (уже есть detail)",
            group.id,
            preferred_id,
        )
    return out


def _saved(existing: dict[str, Vacancy], hh_id: str) -> Vacancy | None:
    """Сохранённая вакансия ядра (копия: окно lookup общее для групп)."""
    v = existing.get(hh_source_post_key(hh_id))
    return v.model_copy(deep=True) if v is not None else None


def _process_group(
    client: HHClient,
    session: Session,
    group: DedupedVacancy,
    by_id: dict[str, VacancyShort],
    result: HhSyncResult,
    buffer: _Buffer,
    existing: dict[str, Vacancy],
    *,
    on_buffered: Callable[[], None],
) -> None:
    canonical_id = group.id
    group_vacancy_id = stable_vacancy_id(canonical_id)
    short = by_id.get(canonical_id)
    if short is None:
        return

    if listings.listing_detail_fetched(session, canonical_id):
        result.details_skipped += 1
        saved = _saved(existing, canonical_id)
        if saved is not None:
            saved.is_duplicate = False
            saved.duplicate_group_id = None
            saved.duplicate_reason = None
            buffer.vacancies.append(saved)
        else:
            _fetch_into_buffer(client, short, result, buffer, existing, is_duplicate=False)
    else:
        _fetch_into_buffer(client, short, result, buffer, existing, is_duplicate=False)
    on_buffered()

    for dup_id, _area in group.duplicates:
        dup_short = by_id.get(dup_id)
        if dup_short is None:
            continue
        _append_duplicate(
            client,
            session,
            dup_short,
            result,
            buffer,
            existing,
            group_vacancy_id=group_vacancy_id,
        )
        on_buffered()


def _append_duplicate(
    client: HHClient,
    session: Session,
    short: VacancyShort,
    result: HhSyncResult,
    buffer: _Buffer,
    existing: dict[str, Vacancy],
    *,
    group_vacancy_id: str,
) -> None:
    """Дубль города: не затирать уже сохранённый detail short-stub'ом."""
    saved = _saved(existing, short.id)
    if saved is not None:
        saved.is_duplicate = True
        saved.duplicate_group_id = group_vacancy_id
        saved.duplicate_reason = "hh_multicity"
        buffer.vacancies.append(saved)
        return
    if listings.listing_detail_fetched(session, short.id):
        # detail был, строки нет — перекачаем, не деградируем до short
        _fetch_into_buffer(
            client,
            short,
            result,
            buffer,
            existing,
            is_duplicate=True,
            duplicate_group_id=group_vacancy_id,
            duplicate_reason="hh_multicity",
        )
        return
    buffer.vacancies.append(
        short_to_vacancy(
            short,
            is_duplicate=True,
            duplicate_group_id=group_vacancy_id,
            duplicate_reason="hh_multicity",
        )
    )


def _fetch_into_buffer(
    client: HHClient,
    short: VacancyShort,
    result: HhSyncResult,
    buffer: _Buffer,
    existing: dict[str, Vacancy],
    *,
    is_duplicate: bool,
    duplicate_group_id: str | None = None,
    duplicate_reason: str | None = None,
) -> None:
    try:
        with _TRACER.start_as_current_span("hh.detail") as span:
            span.set_attribute("hh.vacancy_id", short.id)
            detail = client.get_vacancy(short.id)
        vac = detail_to_vacancy(
            detail,
            is_duplicate=is_duplicate,
            duplicate_group_id=duplicate_group_id,
            duplicate_reason=duplicate_reason,
        )
        # Отметка «детали скачаны» — после записи пачки ядром (flush_buffer).
        buffer.fetched_details.append(short.id)
        result.details_fetched += 1
        buffer.vacancies.append(vac)
    except HHApiError as exc:
        record_hh_api_error(status=str(exc.status_code or "unknown"))
        msg = f"detail {short.id}: HTTP {exc.status_code}"
        result.errors.append(msg)
        _LOG.warning("HH %s", msg)
        saved = _saved(existing, short.id)
        if saved is not None:
            # Повторная загрузка не удалась: сохранённая карточка остаётся (upsert HH перезаписал
            # бы полный текст короткой выдачей). Детали не отмечены — следующий sync попробует снова.
            saved.is_duplicate = is_duplicate
            saved.duplicate_group_id = duplicate_group_id
            saved.duplicate_reason = duplicate_reason
            buffer.vacancies.append(saved)
            return
        buffer.vacancies.append(
            short_to_vacancy(
                short,
                is_duplicate=is_duplicate,
                duplicate_group_id=duplicate_group_id,
                duplicate_reason=duplicate_reason,
            )
        )
