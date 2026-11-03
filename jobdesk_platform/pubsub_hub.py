"""Один Redis-подписчик на процесс для всех SSE-стримов (parse runs, LLM-джобы, HH sync).

Раньше каждое SSE-соединение держало свой ``pubsub`` и занимало поток executor'а
блокирующим ``get_message(timeout=1)``: при подписчиках больше ``cpu+4`` события
доставлялись с растущей задержкой. Здесь одно соединение ``redis.asyncio`` на event
loop: каналы подписываются по счётчику ссылок, читающая задача раскладывает
сообщения по ``asyncio.Queue`` подписчиков.

Читатель живёт, пока есть подписчики. Сбой Redis закрывает хаб и будит всех
подписчиков исключением ``PubSubUnavailable`` — SSE переходит на опрос БД.

Учёт подписчиков — синхронный (без ``await``): при обрыве клиента Starlette
отменяет стрим, и любой ``await`` в его ``finally`` тоже был бы отменён — подписка
утекла бы. Команды Redis на отписку и закрытие выполняет отдельная задача.

Клиент Redis (``redis.asyncio``) создаёт фабрика процесса: ядро и сервисы регистрируют
свою (``set_default_redis_factory``) или передают её хабу явно.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import weakref
from collections.abc import AsyncIterator, Callable
from typing import Any

_LOG = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 256
_READ_TIMEOUT_S = 1.0

RedisFactory = Callable[[], Any]
_default_redis_factory: RedisFactory | None = None


def set_default_redis_factory(factory: RedisFactory | None) -> None:
    """Фабрика ``redis.asyncio``-клиента для хабов без своей фабрики."""
    global _default_redis_factory
    _default_redis_factory = factory


def _resolve_factory(factory: RedisFactory | None) -> RedisFactory:
    resolved = factory or _default_redis_factory
    if resolved is None:
        raise RuntimeError("Redis для pub/sub не настроен (set_default_redis_factory)")
    return resolved


class PubSubUnavailable(RuntimeError):
    """Redis pub/sub недоступен — подписчику нужен fallback на опрос БД."""


_CLOSED = object()  # сигнал подписчикам: хаб закрыт из-за ошибки Redis


class Subscription:
    """Очередь сообщений одного канала для одного SSE-стрима."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    def _put(self, item: Any) -> None:
        if self._queue.full():
            # Медленный клиент: старый прогресс не важен, важен свежий и done.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(item)

    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Следующее сообщение (JSON → dict) или ``None``, если за ``timeout`` ничего не пришло."""
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None
        if item is _CLOSED:
            raise PubSubUnavailable("Redis pub/sub закрыт")
        return item


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class PubSubHub:
    def __init__(self, redis_factory: RedisFactory | None = None) -> None:
        # None — фабрика по умолчанию, берётся в момент подписки (тесты подменяют её источник).
        self._redis_factory = redis_factory
        # Сериализует команды Redis (subscribe / unsubscribe / close).
        self._lock = asyncio.Lock()
        self._subs: dict[str, set[Subscription]] = {}
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        self._reader: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self._subs)

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[Subscription]:
        sub = Subscription()
        async with self._lock:
            try:
                if self._pubsub is None:
                    self._redis = _resolve_factory(self._redis_factory)()
                    self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                if channel not in self._subs:
                    await self._pubsub.subscribe(channel)
                self._subs.setdefault(channel, set()).add(sub)
                if self._reader is None or self._reader.done():
                    self._reader = asyncio.create_task(self._read_loop(self._pubsub), name="sse-pubsub-reader")
            except Exception as e:
                await self._close_locked()
                raise PubSubUnavailable(str(e)) from e
        try:
            yield sub
        finally:
            self._release(channel, sub)

    def _release(self, channel: str, sub: Subscription) -> None:
        """Синхронно убрать подписчика; команду Redis — в фоновую задачу."""
        subs = self._subs.get(channel)
        if subs is None or sub not in subs:
            return
        subs.discard(sub)
        if subs:
            return
        del self._subs[channel]
        task = asyncio.get_running_loop().create_task(self._apply_release(channel))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _apply_release(self, channel: str) -> None:
        async with self._lock:
            if not self._subs:
                await self._close_locked()
            elif channel not in self._subs and self._pubsub is not None:
                try:
                    await self._pubsub.unsubscribe(channel)
                except Exception as e:
                    _LOG.debug("SSE pubsub: unsubscribe %s: %s", channel, e)

    async def _read_loop(self, pubsub: Any) -> None:
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_READ_TIMEOUT_S)
                if not message or message.get("type") != "message":
                    continue
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8", errors="replace")
                payload = _decode(message.get("data"))
                for sub in tuple(self._subs.get(channel, ())):
                    sub._put(dict(payload))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.warning("SSE pubsub: чтение из Redis прервано (%s) — стримы переходят на опрос БД", e)
            async with self._lock:
                if self._pubsub is pubsub:
                    await self._close_locked()

    async def _close_locked(self) -> None:
        """Закрыть соединение и разбудить подписчиков. Вызывать под ``self._lock``."""
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        for subs in self._subs.values():
            for sub in subs:
                sub._put(_CLOSED)
        self._subs.clear()
        pubsub, redis = self._pubsub, self._redis
        self._pubsub = self._redis = None
        for res in (pubsub, redis):
            if res is not None:
                with contextlib.suppress(Exception):
                    await res.aclose()


# Хаб привязан к event loop (asyncio-соединение нельзя делить между циклами).
# В проде цикл один; TestClient может создавать новый на запрос.
# Ключ — имя хаба: в одном процессе (тесты) ядро и сервис держат свои соединения.
_hubs: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, PubSubHub]] = weakref.WeakKeyDictionary()


def get_pubsub_hub(redis_factory: RedisFactory | None = None, *, name: str = "default") -> PubSubHub:
    loop = asyncio.get_running_loop()
    by_name = _hubs.get(loop)
    if by_name is None:
        by_name = _hubs[loop] = {}
    hub = by_name.get(name)
    if hub is None:
        hub = by_name[name] = PubSubHub(redis_factory)
    return hub
