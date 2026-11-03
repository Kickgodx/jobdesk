"""Потоковый run_parse: канал уходит в обработку и в БД, не дожидаясь вычитки остальных."""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

import jobdesk.parse_runner as parse_runner
from jobdesk.config.schema import (
    AiConfig,
    AppConfig,
    FxConfig,
    ParserConfig,
    SourcesConfig,
)
from jobdesk.parse_cancel import ParseCancellationToken, ParseCancelled
from jobdesk.parsers.base import ChannelCheckpoint, RawPost

CHANNELS = ["alpha", "beta"]


def _post(ch: str, i: int) -> RawPost:
    return RawPost(
        channel_username=ch,
        post_id=str(i),
        text=f"Вакансия {ch} #{i}: Python backend developer, remote, зарплата от {300 + i}k, компания {ch}{i}.",
        post_date=datetime.now(UTC),
        post_url=f"https://t.me/{ch}/{i}",
    )


def _cp(i: int) -> ChannelCheckpoint:
    return ChannelCheckpoint(last_message_id=i, covered_from=date(2026, 10, 1))


def _config(channels: list[str] = CHANNELS, **parser_overrides) -> AppConfig:
    return AppConfig(
        sources=SourcesConfig(channels=channels),
        parser=ParserConfig(
            **{
                "db_flush_every_posts": 1000,
                "db_flush_pending_vacancies": 1000,
                "vacancy_score_threshold": 1,
                "vacancy_min_substantive_score": 1,
                "vacancy_fuzzy_duplicate_threshold": 101,
                "fuzzy_text_duplicate_threshold": 101,
                **parser_overrides,
            }
        ),
        fx=FxConfig(enabled=False),
    )


class _StreamingParser:
    """Отдаёт каналы по одному; перед каждым следующим вызывает ``before_next``."""

    def __init__(self, posts_by_channel: dict[str, list[RawPost]], *, before_next=None, fail_after=None):
        self.posts_by_channel = posts_by_channel
        self.before_next = before_next
        self.fail_after = fail_after
        self.cancellation: ParseCancellationToken | None = None
        self.new_checkpoints: dict[str, ChannelCheckpoint] = {}
        self.finished = threading.Event()

    def stream_posts_many(self, channels, on_channel):
        try:
            for n, ch in enumerate(channels):
                if self.cancellation is not None:
                    self.cancellation.check()
                if n and self.before_next is not None:
                    self.before_next(ch)
                if self.fail_after is not None and n == self.fail_after:
                    raise RuntimeError("Telegram: соединение потеряно")
                self.new_checkpoints[ch] = _cp(n + 1)
                on_channel(ch, list(self.posts_by_channel.get(ch, [])), None)
        finally:
            self.finished.set()


def _install(monkeypatch, parser: _StreamingParser) -> None:
    def factory(config, cancellation=None):
        parser.cancellation = cancellation
        return parser

    monkeypatch.setattr(parse_runner, "_channel_parser", factory)


def test_channel_is_flushed_and_checkpointed_before_next_is_fetched(monkeypatch):
    """Вычитка «beta» ждёт, пока «alpha» разобран, записан и получил чекпоинт."""
    events: list[str] = []
    alpha_done = threading.Event()
    timed_out: list[str] = []

    def before_next(ch):
        if not alpha_done.wait(timeout=5):
            timed_out.append(ch)

    def sink(batch):
        events.extend(f"vacancy:{v.source_channel}" for v in batch)

    def checkpoint_sink(cps):
        events.extend(f"checkpoint:{ch}" for ch in cps)
        alpha_done.set()

    snaps = []
    parser = _StreamingParser(
        {"alpha": [_post("alpha", i) for i in range(3)], "beta": [_post("beta", 1)]}, before_next=before_next
    )
    _install(monkeypatch, parser)

    result = parse_runner.run_parse(
        _config(progress_batch_posts=1),
        vacancy_batch_sink=sink,
        checkpoint_sink=checkpoint_sink,
        progress_callback=snaps.append,
    )

    assert timed_out == []  # обработка шла, пока вычитка стояла
    assert events.index("checkpoint:alpha") == 3  # после всех трёх вакансий канала
    assert events[:3] == ["vacancy:alpha"] * 3
    assert "checkpoint:beta" in events
    assert len(result.vacancies_unique) == 4
    assert result.channel_checkpoints == {"alpha": _cp(1), "beta": _cp(2)}
    # Пока «beta» не прочитан, прогресс честно говорит «вычитка идёт».
    first = snaps[0]
    assert (first.phase, first.channels_total, first.channels_fetched, first.posts_total) == ("fetching", 2, 1, 3)


def test_checkpoint_not_moved_when_vacancies_were_not_written(monkeypatch):
    moved = []

    def failing_sink(batch):
        raise RuntimeError("db is down")

    _install(monkeypatch, _StreamingParser({"alpha": [_post("alpha", 1)]}))
    result = parse_runner.run_parse(
        _config(channels=["alpha"]), vacancy_batch_sink=failing_sink, checkpoint_sink=moved.append
    )

    assert moved == []  # вакансии канала не в БД — следующий прогон его перечитает
    assert len(result.vacancies_unique) == 1


def test_fetch_failure_keeps_already_processed_channels(monkeypatch):
    written: list[str] = []
    _install(
        monkeypatch,
        _StreamingParser({"alpha": [_post("alpha", 1), _post("alpha", 2)], "beta": [_post("beta", 1)]}, fail_after=1),
    )

    with pytest.raises(RuntimeError, match="соединение потеряно"):
        parse_runner.run_parse(_config(), vacancy_batch_sink=lambda b: written.extend(v.source_channel for v in b))

    assert written == ["alpha", "alpha"]


def test_processing_error_stops_fetch_without_cancelling_run(monkeypatch):
    channels = [f"ch{i}" for i in range(50)]
    parser = _StreamingParser({ch: [_post(ch, 1)] for ch in channels})
    _install(monkeypatch, parser)

    def boom(post, config, **kwargs):
        raise ValueError("bug in rules")

    monkeypatch.setattr(parse_runner, "process_raw_post", boom)
    cancel = ParseCancellationToken()

    with pytest.raises(ValueError, match="bug in rules"):
        parse_runner.run_parse(_config(channels=channels), cancel=cancel)

    assert parser.finished.wait(timeout=5)  # вычитка остановлена, поток не висит
    assert parser.cancellation.is_cancelled()
    assert not cancel.is_cancelled()  # прогон не помечен отменённым пользователем


def test_user_cancel_reaches_fetch(monkeypatch):
    cancel = ParseCancellationToken()

    def before_next(ch):
        cancel.request_cancel()

    parser = _StreamingParser({"alpha": [_post("alpha", 1)], "beta": [_post("beta", 1)]}, before_next=before_next)
    _install(monkeypatch, parser)

    result = parse_runner.run_parse(_config(), cancel=cancel)

    assert parser.cancellation.is_cancelled()
    assert "Прогон прерван пользователем." in (result.parse_warnings or "")
    assert result.channel_checkpoints == {}


def test_background_fetch_blocks_when_queue_is_full():
    """Очередь на один канал: второй ждёт в put, третий не читается, пока обработка стоит."""
    delivered: list[str] = []

    class Fetcher:
        def stream_posts_many(self, channels, on_channel):
            for ch in channels:
                delivered.append(ch)
                on_channel(ch, [], None)

    fetch = parse_runner._BackgroundFetch(Fetcher(), ["a", "b", "c"], buffer_channels=1)
    fetch.start()
    deadline = time.monotonic() + 5
    while len(delivered) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)
    assert delivered == ["a", "b"]
    assert not fetch.done.is_set()

    fetch.stop(ParseCancellationToken())
    assert fetch.done.is_set()
    assert isinstance(fetch.error, ParseCancelled)


def test_background_fetch_drains_in_order():
    class Fetcher:
        def stream_posts_many(self, channels, on_channel):
            for ch in channels:
                on_channel(ch, [_post(ch, 1)], None)

    fetch = parse_runner._BackgroundFetch(Fetcher(), ["a", "b", "c"], buffer_channels=1)
    fetch.start()
    got = []
    while not fetch.exhausted:
        item = fetch.get(timeout=0.25)
        if item is not None:
            got.append(item[0])
    assert got == ["a", "b", "c"]
    assert fetch.error is None


def test_child_token_follows_parent_but_not_back():
    parent = ParseCancellationToken()
    child = parent.child()
    child.request_cancel()
    assert child.is_cancelled() and not parent.is_cancelled()

    other = parent.child()
    parent.request_cancel()
    assert other.is_cancelled()
    assert parent.child().is_cancelled()  # созданный после отмены — сразу отменён


@pytest.mark.parametrize(
    ("mode", "errors", "rate_limit_skipped", "skipped_limit", "lost"),
    [
        ("primary", 0, 0, 0, False),
        ("primary", 1, 0, 0, True),
        ("primary", 0, 1, 0, True),
        ("primary", 0, 0, 1, True),  # пост сверх ai.max_posts_per_run не сохранён
        ("assist", 3, 3, 3, False),  # в assist такие посты разобраны rules
    ],
)
def test_llm_posts_lost(mode, errors, rate_limit_skipped, skipped_limit, lost):
    ai = SimpleNamespace(
        stats=SimpleNamespace(errors=errors, rate_limit_skipped=rate_limit_skipped, skipped_limit=skipped_limit)
    )
    assert parse_runner._llm_posts_lost(AppConfig(ai=AiConfig(mode=mode)), ai) is lost
    assert parse_runner._llm_posts_lost(AppConfig(ai=AiConfig(mode=mode)), None) is False


class _NeverFetched(_StreamingParser):
    """Парсер, к которому не должны обращаться: прогон обязан упасть до вычитки."""

    def __init__(self):
        super().__init__({})
        self.calls = 0

    def stream_posts_many(self, channels, on_channel):
        self.calls += 1
        return super().stream_posts_many(channels, on_channel)


def _ai_config(mode: str) -> AppConfig:
    cfg = _config()
    return cfg.model_copy(update={"ai": cfg.ai.model_copy(update={"enabled": True, "mode": mode})})


def _broken_ai(monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise ValueError("OPENROUTER_API_KEY не задан")

    monkeypatch.setattr("jobdesk.ai.enricher.AIEnrichmentService", broken)


def _init_failed() -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("llm_enrich_disabled_total", {"reason": "init_failed"}) or 0.0


def test_primary_without_llm_fails_before_fetch(monkeypatch):
    from jobdesk.ai.enricher import LlmUnavailableError

    before = _init_failed()
    _broken_ai(monkeypatch)
    parser = _NeverFetched()
    _install(monkeypatch, parser)

    with pytest.raises(LlmUnavailableError) as exc:
        parse_runner.run_parse(_ai_config("primary"))

    message = str(exc.value)
    assert "OPENROUTER_API_KEY не задан" in message
    assert "primary" in message and "assist" in message
    assert parser.calls == 0
    assert _init_failed() == before + 1


def test_assist_without_llm_runs_on_rules(monkeypatch):
    before = _init_failed()
    _broken_ai(monkeypatch)
    _install(monkeypatch, _StreamingParser({"alpha": [_post("alpha", 1)], "beta": []}))

    result = parse_runner.run_parse(_ai_config("assist"))

    assert len(result.vacancies_unique) == 1
    assert result.vacancies_unique[0].extraction_source == "rules"
    assert "AI не запущен: OPENROUTER_API_KEY не задан" in result.parse_warnings
    assert result.incomplete_reason is None
    assert _init_failed() == before + 1


class _MemCache:
    def __init__(self, hits: set[str]) -> None:
        self.hits = hits

    def get(self, *, text, model, prompt_version, mode):
        from jobdesk.ai.schema import VacancyAIExtraction

        return VacancyAIExtraction(is_job_post=True, confidence=0.9) if text in self.hits else None

    def put(self, **kwargs) -> None:
        pass


def _run_primary_with_limit(monkeypatch, *, limit: int, posts: list[RawPost], cached: set[str]):
    """run_parse в primary с настоящим AIEnrichmentService: подменены только кэш и вызов модели."""
    from jobdesk.ai.clients.openrouter import LLMUsage, OpenRouterClient
    from jobdesk.ai.schema import VacancyAIExtraction

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("jobdesk.ai.cache.build_extraction_cache", lambda ai: _MemCache(cached))
    calls: list[str] = []

    def fake_extract(self, *, text, search_context):
        calls.append(text)
        return VacancyAIExtraction(is_job_post=True, confidence=0.9), LLMUsage(model="m")

    monkeypatch.setattr(OpenRouterClient, "extract_with_fallback", fake_extract)
    _install(monkeypatch, _StreamingParser({"alpha": posts, "beta": []}))
    cfg = _ai_config("primary")
    cfg = cfg.model_copy(update={"ai": cfg.ai.model_copy(update={"max_posts_per_run": limit})})
    return parse_runner.run_parse(cfg, cli_overrides={"api": True}), calls


def test_primary_over_max_posts_per_run_is_incomplete(monkeypatch):
    posts = [_post("alpha", i) for i in range(5)]

    result, calls = _run_primary_with_limit(monkeypatch, limit=2, posts=posts, cached=set())

    assert len(calls) == 2
    assert len(result.vacancies_unique) == 2  # посты сверх потолка не сохранены правилами
    assert "ai.max_posts_per_run=2" in result.incomplete_reason
    assert "не разобрано постов: 3" in result.incomplete_reason
    assert result.channel_checkpoints == {}


def test_cached_answers_do_not_spend_max_posts_per_run(monkeypatch):
    posts = [_post("alpha", i) for i in range(5)]
    # Кэшированные идут последними: раньше потолок проверялся до кэша и отсекал их.
    from jobdesk.ingestion.rules import rules_from_raw_post

    cached = {rules_from_raw_post(p, _config()).cleaned_text for p in posts[2:]}

    result, calls = _run_primary_with_limit(monkeypatch, limit=2, posts=posts, cached=cached)

    assert len(calls) == 2
    assert len(result.vacancies_unique) == 5
    assert result.incomplete_reason is None


def test_long_window_keeps_newest_days_and_warns(monkeypatch):
    from jobdesk.config.schema import SearchConfig

    _install(monkeypatch, _StreamingParser({"alpha": [_post("alpha", 1)]}))
    cfg = _config(channels=["alpha"])
    cfg.search = SearchConfig(date_from="2026-01-01", date_to="2026-10-19", max_days=30)

    result = parse_runner.run_parse(cfg)

    assert "2026-09-20" in (result.parse_warnings or "")
    assert "2026-10-19" in (result.parse_warnings or "")


def _junk(ch: str, i: int) -> RawPost:
    return RawPost(
        channel_username=ch,
        post_id=f"junk{i}",
        text=f"Розыгрыш призов #{i}, подписывайтесь на канал!",
        post_date=datetime.now(UTC),
        post_url=f"https://t.me/{ch}/junk{i}",
    )


def test_summary_lists_channels_with_most_skipped_posts(monkeypatch):
    posts = {
        "alpha": [_post("alpha", 1), _junk("alpha", 1)],
        "beta": [_junk("beta", i) for i in range(3)],
        "gamma": [_post("gamma", 1)],
    }
    _install(monkeypatch, _StreamingParser(posts))
    cfg = _config(channels=list(posts), vacancy_score_threshold=20)

    result = parse_runner.run_parse(cfg)

    summary = result.run_params["parse_summary"]
    assert summary["top_skipped_channels"] == [
        {"channel": "beta", "posts": 3, "skipped": 3},
        {"channel": "alpha", "posts": 2, "skipped": 1},
    ]
    assert summary["top_skipped_channels_incomplete"] is False


def test_cancelled_run_keeps_skipped_channels_without_cancelled_posts(monkeypatch):
    cancel = ParseCancellationToken()
    alpha_done = threading.Event()

    def before_next(ch):
        alpha_done.wait(timeout=5)  # alpha разобран целиком — отмена приходит до beta
        cancel.request_cancel()

    posts = {"alpha": [_junk("alpha", i) for i in range(2)], "beta": [_junk("beta", 1)]}
    _install(monkeypatch, _StreamingParser(posts, before_next=before_next))

    result = parse_runner.run_parse(
        _config(channels=list(posts), vacancy_score_threshold=20),
        cancel=cancel,
        checkpoint_sink=lambda cps: alpha_done.set(),
    )

    # beta уже не разбирался — его посты не засчитаны пропуском.
    assert result.run_params["parse_summary"]["top_skipped_channels"] == [
        {"channel": "alpha", "posts": 2, "skipped": 2}
    ]


def test_skipped_channels_marked_incomplete_when_llm_lost_posts(monkeypatch):
    posts = [_post("alpha", i) for i in range(5)]

    result, _ = _run_primary_with_limit(monkeypatch, limit=2, posts=posts, cached=set())

    summary = result.run_params["parse_summary"]
    assert summary["top_skipped_channels"] == [{"channel": "alpha", "posts": 5, "skipped": 3}]
    assert summary["top_skipped_channels_incomplete"] is True


def test_top_skipped_channels_orders_by_count_then_ratio_then_name():
    posts = {"a": 10, "b": 4, "c": 4, "d": 1, "e": 9, "f": 9, "g": 3, "h": 4}
    skipped = {"a": 4, "b": 4, "c": 2, "d": 0, "e": 3, "f": 2, "g": 2, "h": 2}

    top = parse_runner.top_skipped_channels(posts, skipped, limit=10)

    # b и a — по 4 пропуска, у b доля выше; среди двух пропусков g (2/3) выше c и h (2/4),
    # c и h равны — по имени; f (2/9) последний; d без пропусков не попадает.
    assert [r["channel"] for r in top] == ["b", "a", "e", "g", "c", "h", "f"]
    assert top[0] == {"channel": "b", "posts": 4, "skipped": 4}


def test_top_skipped_channels_limit():
    skipped = {f"ch{i}": i + 1 for i in range(8)}

    top = parse_runner.top_skipped_channels(dict(skipped), skipped)

    assert [r["channel"] for r in top] == ["ch7", "ch6", "ch5", "ch4", "ch3"]
