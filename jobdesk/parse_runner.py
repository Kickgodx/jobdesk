"""Оркестрация: Telethon → извлечение → фильтры → дедуп → метрики."""

from __future__ import annotations

import contextvars
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date
from typing import TYPE_CHECKING, TypedDict

from opentelemetry import context as otel_context
from opentelemetry import trace

from jobdesk.analytics.metrics_builder import build_metrics
from jobdesk.config.schema import AppConfig
from jobdesk.deduplication.deduplicator import VacancyDeduplicator
from jobdesk.fx.vacancy_rub import enrich_vacancies_salary_rub
from jobdesk.ingestion.policy import IngestPolicy
from jobdesk.ingestion.process import process_raw_post
from jobdesk.ingestion.rules import (
    raw_post_to_vacancy,
    raw_post_to_vacancy_rules,
    rules_from_raw_post,
)
from jobdesk.models.parse_result import ParseResult
from jobdesk.models.vacancy import Vacancy
from jobdesk.observability.llm_metrics import record_llm_disabled
from jobdesk.observability.parse_metrics import record_phase_duration, record_post_processed
from jobdesk.parse_cancel import (
    ParseCancellationToken,
    ParseCancelled,
    install_sigint_cancel_handler,
)
from jobdesk.parse_progress import (
    PROGRESS_PREVIEW_LIMIT,
    ParseProgressCallback,
    ParseProgressSnapshot,
)
from jobdesk.parsers.base import (
    BaseChannelParser,
    ChannelCallback,
    ChannelCheckpoint,
    ForwardedPost,
    RawPost,
)
from jobdesk.parsers.channel_username import normalize_channel_username
from jobdesk.utils.dates import (
    clamp_range_days,
    resolve_search_date_from,
    resolve_search_date_to,
)
from jobdesk.utils.env_loader import try_load_dotenv
from jobdesk.utils.logger import get_logger
from jobdesk.utils.sorting import sort_vacancies_for_report
from jobdesk.utils.thread_pool import DaemonThreadPoolExecutor

# Обратная совместимость: тесты/импорты из parse_runner.
__all__ = [
    "raw_post_to_vacancy",
    "raw_post_to_vacancy_rules",
    "rules_from_raw_post",
    "run_parse",
]

if TYPE_CHECKING:
    from jobdesk.ai.enricher import AIEnrichmentService

_LOG = get_logger(__name__)
_TRACER = trace.get_tracer(__name__)

# Сколько каналов с наибольшим числом пропущенных постов показывать в итоге прогона.
TOP_SKIPPED_CHANNELS_LIMIT = 5


class SkippedChannel(TypedDict):
    channel: str
    posts: int
    skipped: int


def top_skipped_channels(
    posts_by_channel: dict[str, int],
    skipped_by_channel: dict[str, int],
    *,
    limit: int = TOP_SKIPPED_CHANNELS_LIMIT,
) -> list[SkippedChannel]:
    """Каналы с наибольшим числом пропущенных постов (не стали вакансией после фильтров).

    Порядок — по числу пропусков, при равенстве — по доле пропусков, затем по имени.
    Каналы без пропусков не попадают: список подсказывает, какие источники мусорные.
    """
    rows = [
        (ch, posts_by_channel.get(ch, skipped), skipped) for ch, skipped in skipped_by_channel.items() if skipped > 0
    ]
    rows.sort(key=lambda r: (-r[2], -r[2] / max(1, r[1]), r[0]))
    return [SkippedChannel(channel=ch, posts=posts, skipped=skipped) for ch, posts, skipped in rows[:limit]]


def _channel_parser(
    config: AppConfig,
    *,
    cancellation: ParseCancellationToken | None = None,
) -> BaseChannelParser:
    """Единственный источник сообщений — Telethon (MTProto с файлом сессии)."""
    from jobdesk.parsers.telegram_telethon import (
        TelegramTelethonParser,
        telethon_import_error,
    )

    if err := telethon_import_error():
        raise RuntimeError(
            "Нужен Telethon: poetry install (и при необходимости --extras telethon). "
            "Сессия: poetry run python -m jobdesk.cli login"
        ) from err
    return TelegramTelethonParser(config, cancellation=cancellation)


def _build_parse_result(
    *,
    config: AppConfig,
    cli_overrides: dict | None,
    all_warnings: list[str],
    posts_fetched: int,
    candidates: list[Vacancy],
    unique: list[Vacancy],
    dups: list[Vacancy],
    dedup_elapsed_sec: float,
    ai_service: AIEnrichmentService | None,
    posts_processed: int = 0,
    fetch_elapsed_sec: float = 0.0,
    process_elapsed_sec: float = 0.0,
    fx_rates: dict[date, dict[str, float]] | None = None,
    incomplete_reason: str | None = None,
    channel_checkpoints: dict[str, ChannelCheckpoint] | None = None,
    total_elapsed_sec: float | None = None,
    forwarded_posts: list[ForwardedPost] | None = None,
    skipped_channels: list[SkippedChannel] | None = None,
) -> ParseResult:
    _LOG.info(
        "Дедупликация: %s уникальных, %s дубликатов за %.1f с.",
        len(unique),
        len(dups),
        dedup_elapsed_sec,
    )

    sort_vacancies_for_report(candidates)
    sort_vacancies_for_report(unique)
    sort_vacancies_for_report(dups)

    if config.fx.enabled:
        enrich_started = time.monotonic()
        enrich_vacancies_salary_rub(
            unique,
            default_currency_if_missing=config.fx.default_currency_if_missing,
            request_timeout_seconds=config.fx.request_timeout_seconds,
            rates_for_date=fx_rates,
        )
        _LOG.info(
            "Курсы для метрик прогона: %s вакансий за %.1f с%s.",
            len(unique),
            time.monotonic() - enrich_started,
            " (из БД)" if fx_rates else "",
        )

    metrics = build_metrics(
        vacancies_all=candidates,
        vacancies_unique=unique,
        duplicates=dups,
        posts_collected=posts_fetched,
    )
    metrics.warnings.extend(all_warnings)

    posts_skipped = max(0, posts_processed - len(candidates))
    if total_elapsed_sec is None:
        total_elapsed_sec = fetch_elapsed_sec + process_elapsed_sec + dedup_elapsed_sec

    run_params = config.to_serializable_dict()
    run_params["parse_summary"] = {
        "total_elapsed_seconds": round(total_elapsed_sec, 1),
        "fetch_elapsed_seconds": round(fetch_elapsed_sec, 1),
        "process_elapsed_seconds": round(process_elapsed_sec, 1),
        "dedup_elapsed_seconds": round(dedup_elapsed_sec, 1),
        "posts_fetched": posts_fetched,
        "posts_processed": posts_processed,
        "posts_skipped": posts_skipped,
        "vacancy_candidates": len(candidates),
        "top_skipped_channels": skipped_channels or [],
        # Посты, потерянные из-за LLM, неотличимы от мусора: список тогда лишь оценка.
        "top_skipped_channels_incomplete": _llm_posts_lost(config, ai_service),
    }
    if ai_service is not None:
        run_params["ai_stats"] = ai_service.stats.to_dict(
            catalog=config.llm_model_catalog,
            primary_model=config.ai.model,
        )
    if cli_overrides:
        run_params["cli_overrides"] = cli_overrides

    parse_warnings = "; ".join(all_warnings) if all_warnings else None

    return ParseResult(
        run_params=run_params,
        posts_fetched=posts_fetched,
        parse_warnings=parse_warnings,
        vacancies_all=candidates,
        vacancies_unique=unique,
        duplicates=dups,
        metrics=metrics,
        incomplete_reason=incomplete_reason,
        channel_checkpoints=channel_checkpoints or {},
        forwarded_posts=forwarded_posts or [],
    )


_FETCH_JOIN_TIMEOUT_S = 10.0


def _submit_in_context(pool: ThreadPoolExecutor, process_one, post) -> Future:
    """Задача в пул с копией контекста: воркеры наследуют текущий OTel-span.

    LLM-вызовы — дети ``parse.process_posts``, а не отдельные трассы; один Context на задачу.
    """
    return pool.submit(contextvars.copy_context().run, process_one, post)


def _stream_channels(fetcher, channels: list[str], on_channel: ChannelCallback) -> None:
    stream = getattr(fetcher, "stream_posts_many", None)
    if stream is None:
        # Парсер без потокового режима (заглушки в тестах): весь пакет разом.
        for ch, posts, warn in fetcher.fetch_posts_many(channels):
            on_channel(ch, posts, warn)
        return
    stream(channels, on_channel)


class _BackgroundFetch:
    """Вычитка каналов в фоновом потоке → ограниченная очередь → обработка.

    Канал попадает в очередь целиком, как только прочитан. Очередь рассчитана на
    ``buffer_channels`` каналов: пока обработка не забрала прочитанное, поток вычитки
    ждёт в ``put`` (Telethon — не отпуская слот семафора), так что в памяти держится
    несколько каналов, а не всё окно прогона.
    """

    def __init__(self, fetcher, channels: list[str], *, buffer_channels: int) -> None:
        self._fetcher = fetcher
        self._channels = channels
        self._queue: queue.Queue[tuple[str, list[RawPost], str | None]] = queue.Queue(maxsize=max(1, buffer_channels))
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._posts_fetched = 0
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.elapsed = 0.0
        self._thread = threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._run,),
            name="parse-fetch",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _put(self, channel: str, posts: list[RawPost], warning: str | None) -> None:
        with self._lock:
            self._posts_fetched += len(posts)
        while not self._closed.is_set():
            try:
                self._queue.put((channel, posts, warning), timeout=0.25)
                return
            except queue.Full:
                continue
        raise ParseCancelled()

    def _run(self) -> None:
        started = time.monotonic()
        try:
            with _TRACER.start_as_current_span(
                "parse.fetch_posts", attributes={"parse.channels": len(self._channels)}
            ) as span:
                _stream_channels(self._fetcher, self._channels, self._put)
                span.set_attribute("parse.posts_fetched", self._posts_fetched)
        except BaseException as e:  # передаётся в поток обработки, там и поднимается
            self.error = e
        finally:
            self.elapsed = time.monotonic() - started
            self.done.set()

    def get(self, timeout: float | None = None) -> tuple[str, list[RawPost], str | None] | None:
        """Следующий прочитанный канал; ``None`` — пока нечего (без ``timeout`` не ждёт)."""
        try:
            return self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        except queue.Empty:
            return None

    @property
    def exhausted(self) -> bool:
        # done ставится после последнего put — пустая очередь при done уже не пополнится.
        return self.done.is_set() and self._queue.empty()

    def stop(self, fetch_cancel: ParseCancellationToken) -> None:
        """Остановить вычитку (отмена или ошибка обработки) и дождаться потока."""
        self._closed.set()
        if self._thread.ident is None:
            return
        if not self.done.is_set():
            fetch_cancel.request_cancel()
        self._thread.join(timeout=_FETCH_JOIN_TIMEOUT_S)


def _llm_posts_lost(config: AppConfig, ai_service: AIEnrichmentService | None) -> bool:
    """Отброшен ли из-за LLM хоть один пост — тогда чекпоинты двигать нельзя.

    В ``primary`` теряется пост с ошибкой LLM и пост сверх ``ai.max_posts_per_run``;
    вне ``assist`` — и пост, пропущенный из-за отключения LLM (лимит / квота). Те же
    условия, что у ``incomplete_reason``.
    """
    if ai_service is None:
        return False
    stats = ai_service.stats
    if config.ai.mode == "primary" and (stats.errors or stats.skipped_limit):
        return True
    return config.ai.mode != "assist" and bool(stats.rate_limit_skipped)


def _end_span(span: trace.Span, ctx_token) -> None:
    otel_context.detach(ctx_token)
    span.end()


def _checkpoints_if_fully_processed(
    channel_fetcher,
    *,
    config: AppConfig,
    ai_service: AIEnrichmentService | None,
    incomplete_reason: str | None,
    all_warnings: list[str],
) -> dict[str, ChannelCheckpoint]:
    """Новые чекпоинты каналов — только если ни один прочитанный пост не потерян.

    Сдвинутый чекпоинт означает «эти сообщения больше не читать»: пост, отброшенный
    из-за сбоя LLM в ``primary``, следующий прогон уже не увидел бы.
    """
    new = dict(getattr(channel_fetcher, "new_checkpoints", None) or {})
    if not new or incomplete_reason:
        return {}
    if ai_service is not None and config.ai.mode == "primary" and ai_service.stats.errors:
        all_warnings.append(
            f"Чекпоинты каналов не сдвинуты: {ai_service.stats.errors} постов не разобраны LLM "
            "(режим primary) — следующий прогон перечитает окно."
        )
        return {}
    return new


def _llm_call_limit_reason(config: AppConfig, ai_service: AIEnrichmentService) -> str | None:
    """Причина неполного прогона, если в ``primary`` посты не разобраны из-за ``ai.max_posts_per_run``.

    В ``primary`` такой пост не сохраняется с полями правил (см. ``AIEnrichmentService.enrich``).
    """
    skipped = ai_service.stats.skipped_limit
    if config.ai.mode != "primary" or not skipped:
        return None
    return (
        f"AI: достигнут лимит ai.max_posts_per_run={config.ai.max_posts_per_run}; в режиме primary "
        f"не разобрано постов: {skipped}. Они не кэшируются — повторный прогон обработает их."
    )


def _llm_error_ratio_reason(config: AppConfig, ai_service: AIEnrichmentService) -> str | None:
    """Причина неполного прогона, если в ``primary`` доля ошибок LLM выше ``ai.max_error_ratio``.

    В ``primary`` пост с ошибкой LLM отбрасывается: без порога прогон с половиной
    потерянных постов выглядел бы ``completed``.
    """
    if config.ai.mode != "primary":
        return None
    errors = ai_service.stats.errors
    calls = ai_service.stats.calls
    if not errors or not calls:
        return None
    ratio = errors / calls
    if ratio <= config.ai.max_error_ratio:
        return None
    return (
        f"AI: ошибок LLM {errors} из {calls} запросов ({ratio:.0%} > {config.ai.max_error_ratio:.0%}); "
        "в режиме primary посты с ошибкой не разобраны — повторный прогон перечитает окно."
    )


def _api_run(cli_overrides: dict | None) -> bool:
    """Прогон через REST API — служебные строки не дублируем в parse_warnings (есть run_params / UI)."""
    return bool(cli_overrides and cli_overrides.get("api"))


def run_parse(
    config: AppConfig,
    cli_overrides: dict | None = None,
    *,
    progress_callback: ParseProgressCallback | None = None,
    progress_every_n_posts: int | None = None,
    cancel: ParseCancellationToken | None = None,
    vacancy_batch_sink: Callable[[list[Vacancy]], None] | None = None,
    fx_rates: dict[date, dict[str, float]] | None = None,
    channel_checkpoints: dict[str, ChannelCheckpoint] | None = None,
    checkpoint_sink: Callable[[dict[str, ChannelCheckpoint]], None] | None = None,
    known_vacancies: list[Vacancy] | None = None,
) -> ParseResult:
    """
    Основной сценарий MVP.

    Вычитка и обработка идут потоком: каналы читаются в фоновом потоке и уходят в
    обработку по мере готовности (ограниченная очередь, см. ``_BackgroundFetch``) —
    в памяти не копится всё окно, а прочитанное до сбоя успевает попасть в БД.

    ``cli_overrides`` — простой dict для сериализации в ``run_params`` (опционально).
    ``vacancy_batch_sink`` — опциональный колбэк: вызывается с очередной порцией
    вакансий каждые ``parser.db_flush_every_posts`` обработанных постов,
    при накоплении ``parser.db_flush_pending_vacancies`` незаписанных
    (что наступит раньше) и в конце прогона — чтобы сохранять вакансии в БД по ходу
    обработки, а не только в конце. В порции — уникальные и дубли в порядке дедупа
    (у дубля ``is_duplicate`` и ``duplicate_of``): дубли нужны для учёта появлений.
    ``fx_rates`` — готовые курсы (из БД); если заданы, метрики не ходят в ЦБ.
    ``channel_checkpoints`` — чекпоинты каналов прошлых прогонов (из БД) для
    инкрементальной вычитки; новые возвращаются в ``ParseResult.channel_checkpoints``
    только если все прочитанные посты разобраны (см. ``parser.incremental_fetch``).
    ``checkpoint_sink`` — опционально: чекпоинт канала отдаётся сразу, как только
    его посты разобраны и записаны через ``vacancy_batch_sink``, пока ни один пост
    прогона не потерян из-за LLM. Повторный прогон после падения не перечитывает
    такие каналы.
    ``known_vacancies`` — вакансии прошлых прогонов (из БД) для дедупа: перепост с правками
    становится их дублем (``duplicate_of`` — id в БД), а не новой вакансией.
    """
    try_load_dotenv()

    if cancel is not None:
        install_sigint_cancel_handler(cancel, log=_LOG.warning)

    # Свой токен у вычитки: сбой обработки останавливает её, не помечая прогон отменённым.
    fetch_cancel = cancel.child() if cancel is not None else ParseCancellationToken()
    channel_fetcher = _channel_parser(config, cancellation=fetch_cancel)
    if channel_checkpoints and isinstance(channel_fetcher, BaseChannelParser):
        channel_fetcher.checkpoints = dict(channel_checkpoints)

    date_from = resolve_search_date_from(config.search.date_from)
    date_to = resolve_search_date_to(config.search.date_to)

    all_warnings: list[str] = []

    if date_from and date_to:
        span = (date_to - date_from).days + 1
        cap = min(config.search.max_days, 365)
        df_clamped, dt_clamped = clamp_range_days(date_from, date_to, cap)
        if span > cap:
            all_warnings.append(
                f"Запрошенный период {span} дн. обрезан до {cap} дн. "
                f"({df_clamped.isoformat()} — {dt_clamped.isoformat()})."
            )
        config = config.model_copy(
            update={
                "search": config.search.model_copy(
                    update={
                        "date_from": df_clamped.isoformat(),
                        "date_to": dt_clamped.isoformat(),
                    }
                )
            }
        )
        date_from, date_to = df_clamped, dt_clamped

    if not _api_run(cli_overrides):
        all_warnings.append(
            "Telegram: Telethon (MTProto). Убедитесь, что активна сессия и заданы TELEGRAM_API_ID / TELEGRAM_API_HASH."
        )

    # Предвычисляем все словари один раз — иначе они пересобирались бы на каждом из тысяч постов.
    specs_map = config.merged_specializations()
    tech_list = config.merged_technologies()
    wf_map = config.merged_work_format_synonyms()
    exclude_kw = config.merged_exclude_keywords()
    threshold = config.parser.vacancy_score_threshold

    run_started = time.monotonic()
    posts_fetched = 0
    posts_processed = 0
    channels_fetched = 0
    candidates: list[Vacancy] = []
    forwarded: list[ForwardedPost] = []
    # Разобранные и пропущенные (не ставшие кандидатом) посты по каналам — для итога прогона.
    posts_by_channel: dict[str, int] = {}
    skipped_by_channel: dict[str, int] = {}
    batch_step = progress_every_n_posts or config.parser.progress_batch_posts

    channels_unique: list[str] = []
    for ch in config.sources.channels:
        u = normalize_channel_username(ch)
        if u and u not in channels_unique:
            channels_unique.append(u)

    ai_service: AIEnrichmentService | None = None
    if config.ai.enabled:
        from jobdesk.ai.enricher import AIEnrichmentService

        try:
            ai_service = AIEnrichmentService(config)
            # Размер батча для логов растёт по мере вычитки (add_to_batch в _take_channel).
            ai_service.begin_batch(0)
            if not _api_run(cli_overrides):
                all_warnings.append(
                    f"AI: OpenRouter, модель {config.ai.model}"
                    + (
                        f", fallback [{', '.join(config.ai.resolved_fallback_models())}]"
                        if config.ai.resolved_fallback_models()
                        else ""
                    )
                    + f", режим {config.ai.mode}, concurrency={config.ai.concurrency}."
                )
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("openai").setLevel(logging.WARNING)
            _LOG.info(
                "AI-обогащение включено (до дедупликации): модель %s, кэш %s (%s), concurrency=%s.",
                config.ai.model,
                "вкл" if config.ai.use_cache else "выкл",
                config.ai.cache_backend,
                config.ai.concurrency,
            )
        except Exception as e:
            record_llm_disabled("init_failed")
            if config.ai.mode == "primary":
                # Без LLM primary сохранил бы одни правила и сдвинул чекпоинты — эти посты
                # никто бы не перечитал. Вычитка ещё не начата: падаем до Telegram.
                from jobdesk.ai.enricher import LlmUnavailableError

                raise LlmUnavailableError(
                    f"LLM не запущен: {e}. Режим ai.mode=primary требует LLM — исправьте "
                    "конфигурацию LLM (ключ, модель) или запустите прогон без AI либо в режиме assist."
                ) from e
            all_warnings.append(f"AI не запущен: {e}")
            _LOG.error("AI не инициализирован: %s", e)

    process_elapsed = 0.0
    deduplicator = VacancyDeduplicator(config)
    if known_vacancies:
        deduplicator.seed(known_vacancies)
    dedup_elapsed = 0.0
    db_flush_step = max(1, config.parser.db_flush_every_posts)
    db_flush_pending = max(1, config.parser.db_flush_pending_vacancies)
    pending: list[Vacancy] = []  # вакансии (уникальные и дубли), ещё не ушедшие в sink
    incomplete_reason: str | None = None
    fetch = _BackgroundFetch(
        channel_fetcher,
        channels_unique,
        buffer_channels=config.parser.channel_concurrency,
    )
    process_span: trace.Span | None = None
    process_ctx_token = None
    pool: ThreadPoolExecutor | None = None
    try:

        def _register_candidate(v: Vacancy) -> None:
            """Кандидат прошёл фильтры — сразу прогоняем через инкрементальный дедуп."""
            nonlocal dedup_elapsed
            candidates.append(v)
            started = time.monotonic()
            deduplicator.add((v,))
            dedup_elapsed += time.monotonic() - started
            if vacancy_batch_sink is not None:
                pending.append(v)

        def _flush_pending() -> None:
            """
            Отдать накопленные вакансии (уникальные и дубли) в ``vacancy_batch_sink``.

            Ошибки записи глотаем: прогон не должен падать из-за БД — незаписанные
            вакансии остаются в pending и будут предложены повторно на следующем
            флаше / финальным UPSERT в конце прогона.
            """
            if vacancy_batch_sink is None or not pending:
                return
            try:
                with _TRACER.start_as_current_span(
                    "parse.flush_vacancies", attributes={"parse.vacancies": len(pending)}
                ):
                    vacancy_batch_sink(list(pending))
            except ParseCancelled:
                raise
            except Exception as e:
                _LOG.warning(
                    "Промежуточная запись %s вакансий в БД не удалась: %s — повторим позже.",
                    len(pending),
                    e,
                )
                return
            pending.clear()

        tg_policy = IngestPolicy(
            apply_post_filters=True,
            score_threshold_without_ai=threshold if ai_service is None else None,
        )

        def _process_one(post: RawPost) -> Vacancy | None:
            return process_raw_post(
                post,
                config,
                policy=tg_policy,
                ai_service=ai_service,
                spec_map=specs_map,
                tech_list=tech_list,
                wf_map=wf_map,
                exclude_keywords=exclude_kw,
                cancel=cancel,
            )

        def _emit_progress(*, phase: str | None = None) -> None:
            if not progress_callback:
                return
            rl = ai_service._client.rate_limiter if ai_service is not None else None
            # В UI/SSE отдаём уникальные (инкрементальный дедуп уже прогнан):
            # candidates могут делить один content-id → React key warning.
            # Превью нужен только хвост — копия всех уникальных на каждом тике не нужна.
            unique_so_far = deduplicator.unique
            snap = ParseProgressSnapshot(
                posts_processed=posts_processed,
                posts_total=posts_fetched,
                vacancies_found=len(unique_so_far),
                vacancies=unique_so_far[-PROGRESS_PREVIEW_LIMIT:],
                phase=phase or ("processing" if fetch.done.is_set() else "fetching"),
                channels_total=len(channels_unique),
                channels_fetched=channels_fetched,
                ai_llm_disabled=bool(rl and rl.llm_disabled),
                ai_llm_disabled_reason=rl.llm_disabled_reason if rl else None,
                ai_tokens_in=ai_service.stats.tokens_in if ai_service else 0,
                ai_tokens_out=ai_service.stats.tokens_out if ai_service else 0,
                ai_skipped_score=ai_service.stats.skipped_score if ai_service else 0,
            )
            progress_callback(snap)

        # Пост-обработка: regex, с LLM — в пуле потоков. Только regex под GIL пул
        # не ускоряет: разбираем в этом потоке, пока фоновый читает каналы.
        inline = ai_service is None
        workers = 1 if inline else max(1, min(config.ai.concurrency, 16))
        max_inflight = workers * 2
        backlog: deque[tuple[str, RawPost]] = deque()
        remaining: dict[str, int] = {}  # канал → постов, ещё не разобранных
        checkpoints_frozen = False

        def _channel_done(ch: str) -> None:
            """Канал разобран целиком: записать его вакансии и сразу сдвинуть чекпоинт.

            Консервативно: после первого поста, потерянного из-за LLM, чекпоинты по ходу
            прогона больше не двигаются — счётчики ошибок общие на прогон.
            """
            nonlocal checkpoints_frozen
            if checkpoint_sink is None or checkpoints_frozen:
                return
            cp = (getattr(channel_fetcher, "new_checkpoints", None) or {}).get(ch)
            if cp is None:
                return
            if _llm_posts_lost(config, ai_service):
                checkpoints_frozen = True
                return
            _flush_pending()
            if pending:
                return  # запись не удалась — канал перечитает следующий прогон
            try:
                checkpoint_sink({ch: cp})
            except ParseCancelled:
                raise
            except Exception as e:
                _LOG.warning("Чекпоинт канала @%s не записан: %s", ch, e)

        def _take_channel(item: tuple[str, list[RawPost], str | None]) -> None:
            nonlocal posts_fetched, channels_fetched
            ch, posts, warn = item
            channels_fetched += 1
            posts_fetched += len(posts)
            if warn:
                all_warnings.append(warn)
                _LOG.warning("Канал @%s: %s", ch, warn)
            if ai_service is not None:
                ai_service.add_to_batch(len(posts))
            if not posts:
                _channel_done(ch)
                return
            remaining[ch] = remaining.get(ch, 0) + len(posts)
            backlog.extend((ch, p) for p in posts)

        def _on_processed(ch: str, post: RawPost, v: Vacancy | None) -> None:
            nonlocal posts_processed
            posts_processed += 1
            record_post_processed(candidate=v is not None)
            # После отмены разбор возвращает None, не глядя на пост — это не пропуск канала.
            if v is not None or cancel is None or not cancel.is_cancelled():
                posts_by_channel[ch] = posts_by_channel.get(ch, 0) + 1
                if v is None:
                    skipped_by_channel[ch] = skipped_by_channel.get(ch, 0) + 1
            src = post.forward_from
            if src is not None and src.username != ch.lower():
                forwarded.append(
                    ForwardedPost(
                        channel=ch, post_id=post.post_id, post_date=post.post_date, source=src, is_vacancy=v is not None
                    )
                )
            if v is not None:
                _register_candidate(v)
            if posts_processed % batch_step == 0:
                _emit_progress()
            if len(pending) >= db_flush_pending or posts_processed % db_flush_step == 0:
                _flush_pending()
            if posts_processed % 500 == 0:
                _LOG.info(
                    "Обработано постов: %s из %s прочитанных; прошло вакансий: %s",
                    posts_processed,
                    posts_fetched,
                    len(candidates),
                )
            remaining[ch] -= 1
            if remaining[ch] == 0:
                del remaining[ch]
                _channel_done(ch)

        process_span = _TRACER.start_span(
            "parse.process_posts",
            attributes={
                "parse.workers": workers,
                "parse.ai_enabled": ai_service is not None,
            },
        )
        process_ctx_token = otel_context.attach(trace.set_span_in_context(process_span))
        process_started = time.monotonic()
        _LOG.info(
            "Потоковая обработка %s каналов: %s, потоков: %s.",
            len(channels_unique),
            "regex+AI" if ai_service else "regex",
            workers,
        )
        if cancel:
            cancel.check()
        fetch.start()
        if not inline:
            pool = DaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix="post-proc")
        futures: dict[Future, tuple[str, RawPost]] = {}
        while True:
            if cancel:
                cancel.check()
            # Новые каналы забираем, только пока своя очередь мала: иначе прочитанное
            # копилось бы здесь, а не тормозило вычитку.
            while len(backlog) < max_inflight and (item := fetch.get()) is not None:
                _take_channel(item)
            if inline and backlog:
                ch, post = backlog.popleft()
                _on_processed(ch, post, _process_one(post))
                continue
            while pool is not None and backlog and len(futures) < max_inflight:
                ch, post = backlog.popleft()
                futures[_submit_in_context(pool, _process_one, post)] = (ch, post)
            if futures:
                done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
                for fut in done:
                    ch, post = futures.pop(fut)
                    _on_processed(ch, post, fut.result())
                continue
            if fetch.exhausted:
                break
            if (item := fetch.get(timeout=0.25)) is not None:
                _take_channel(item)
        if cancel:
            cancel.check()
        if fetch.error is not None:
            if isinstance(fetch.error, ParseCancelled):
                raise ParseCancelled()
            # Прочитанное до сбоя уже разобрано — записываем, затем прогон падает как раньше.
            _flush_pending()
            raise fetch.error

        fetch_elapsed = fetch.elapsed
        record_phase_duration("fetch", fetch_elapsed)
        _LOG.info(
            "Выборка завершена: %s постов с %s каналов за %.1f с.",
            posts_fetched,
            len(channels_unique),
            fetch_elapsed,
        )
        process_elapsed = time.monotonic() - process_started
        record_phase_duration("process", process_elapsed)
        record_phase_duration("dedup", dedup_elapsed)
        process_span.set_attribute("parse.posts_total", posts_fetched)
        process_span.set_attribute("parse.vacancies_found", len(candidates))
        process_span.set_attribute("parse.vacancies_unique", len(deduplicator.unique))
        _end_span(process_span, process_ctx_token)
        process_span = process_ctx_token = None
        _emit_progress(phase="post_processing_done")
        if ai_service is not None:
            ai_service.log_batch_finished()
            if not _api_run(cli_overrides):
                all_warnings.append(ai_service.stats.format_token_summary())
                cost_summary = ai_service.stats.format_cost_summary(
                    config.llm_model_catalog,
                    config.ai.model,
                )
                if cost_summary:
                    all_warnings.append(cost_summary)
            rate_limiter = ai_service._client.rate_limiter
            skipped = ai_service.stats.rate_limit_skipped
            if skipped and config.ai.mode != "assist":
                # В primary посты без LLM отбрасываются (см. AIEnrichmentService.enrich):
                # прогон неполный, «completed» скрыл бы потерю остатка.
                incomplete_reason = (
                    f"AI: LLM отключён ({rate_limiter.llm_disabled_reason}); в режиме "
                    f"{config.ai.mode} не разобрано постов: {skipped}. Они не кэшируются — "
                    "повторный прогон после сброса лимита обработает их."
                )
                # Прогон через API показывает причину как error_message — не дублируем.
                if not _api_run(cli_overrides):
                    all_warnings.append(incomplete_reason)
            elif rate_limiter.llm_disabled:
                all_warnings.append(
                    f"AI: LLM отключён ({rate_limiter.llm_disabled_reason}). Оставшиеся посты обработаны только rules."
                )
            if incomplete_reason is None:
                incomplete_reason = _llm_call_limit_reason(config, ai_service)
                if incomplete_reason and not _api_run(cli_overrides):
                    all_warnings.append(incomplete_reason)
            if incomplete_reason is None:
                incomplete_reason = _llm_error_ratio_reason(config, ai_service)
                if incomplete_reason and not _api_run(cli_overrides):
                    all_warnings.append(incomplete_reason)
        _LOG.info(
            "Пост-обработка завершена за %.1f с. После фильтров осталось вакансий-кандидатов: %s",
            process_elapsed,
            len(candidates),
        )

        new_checkpoints = _checkpoints_if_fully_processed(
            channel_fetcher,
            config=config,
            ai_service=ai_service,
            incomplete_reason=incomplete_reason,
            all_warnings=all_warnings,
        )

        _flush_pending()
        return _build_parse_result(
            config=config,
            cli_overrides=cli_overrides,
            all_warnings=all_warnings,
            posts_fetched=posts_fetched,
            candidates=candidates,
            unique=deduplicator.unique,
            dups=deduplicator.duplicates,
            dedup_elapsed_sec=dedup_elapsed,
            ai_service=ai_service,
            posts_processed=posts_processed,
            fetch_elapsed_sec=fetch_elapsed,
            process_elapsed_sec=process_elapsed,
            fx_rates=fx_rates,
            incomplete_reason=incomplete_reason,
            channel_checkpoints=new_checkpoints,
            total_elapsed_sec=time.monotonic() - run_started,
            forwarded_posts=forwarded,
            skipped_channels=top_skipped_channels(posts_by_channel, skipped_by_channel),
        )
    except ParseCancelled:
        _LOG.warning("Прогон прерван (отмена).")
        all_warnings.append("Прогон прерван пользователем.")
        try:
            _flush_pending()
        except Exception as flush_err:
            _LOG.warning("Финальный сброс вакансий при отмене не удался: %s", flush_err)
        return _build_parse_result(
            config=config,
            cli_overrides=cli_overrides,
            all_warnings=all_warnings,
            posts_fetched=posts_fetched,
            candidates=candidates,
            unique=deduplicator.unique,
            dups=deduplicator.duplicates,
            dedup_elapsed_sec=dedup_elapsed,
            ai_service=ai_service,
            posts_processed=posts_processed,
            fetch_elapsed_sec=fetch.elapsed,
            process_elapsed_sec=process_elapsed,
            fx_rates=fx_rates,
            total_elapsed_sec=time.monotonic() - run_started,
            forwarded_posts=forwarded,
            skipped_channels=top_skipped_channels(posts_by_channel, skipped_by_channel),
        )
    except KeyboardInterrupt:
        if cancel is not None:
            cancel.request_cancel()
        _LOG.warning("Прогон прерван (Ctrl+C).")
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        fetch.stop(fetch_cancel)
        if process_span is not None:
            process_span.set_attribute("parse.interrupted", True)
            _end_span(process_span, process_ctx_token)
        if ai_service is not None:
            ai_service.close()
