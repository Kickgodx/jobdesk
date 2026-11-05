# Design

## Context

Мотивация — в proposal.md. Сейчас HH sync — это:

- `backend/services/hh_sync_service.py` (поиск → listings → дедуп → детали → пачки → purge),
  `hh_sync_job_service.py` (постановка, отмена), `hh_sync_window.py`, `hh_sync_events.py`;
- `backend/workers/hh_sync_tasks.py`, `hh_worker.py`, `recover_hh_sync.py` (claim, lease, recover);
- `backend/db/hh_repository.py` (listings, advisory lock sync, `upsert_hh_vacancies`,
  `get_vacancy_by_source_post_key`, бэкфилл грейдов), `hh_sync_job_repository.py`;
- `backend/api/routes/hh.py` (6 эндпоинтов, SSE, idempotency);
- хвосты: HH-метрики и гейджи в `backend/observability.py`, purge в `db_cleanup.py` и
  `main.py`, настройки `HH_*` в `backend/settings.py`.

Касания таблиц ядра у HH ровно две: чтение вакансии по `source_post_key` (канон с
деталями, дубль, ошибка деталей) и запись пачки через `upsert_hh_vacancies` с FX. Остальное —
собственные `hh_listings` и `hh_sync_jobs` (FK только `requested_by → users`).

Решения пользователя: данные сервиса — своя схема в той же базе; UI ходит к сервису через
основной API; LLM-шлюз (этап 2) — OpenAI-совместимый прокси.

## Goals / Non-Goals

**Goals:**
- HH-код живёт в `jobdesk_hh` и не импортирует `backend`; `backend` не импортирует
  `jobdesk_hh`. Граница проверяется тестом.
- Образец выноса, который этапы 2–3 повторяют без новых решений: сервисный токен,
  клиент между сервисами, прокси публичного эндпоинта, своя схема и цепочка миграций.
- Публичный контракт `/api/v1/hh/*` не меняется: frontend и тесты контракта те же.

**Non-Goals:**
- Отдельная база или сервер Postgres на сервис; разные зависимости (`poetry.lock`) на сервис.
- Перенос `backend/db/session.py` ядра на общий пакет: ядро сохраняет свои пулы по ролям.
- Асинхронная доставка вакансий (очередь событий) — хватает синхронного HTTP с повторами.

## Decisions

### D1. Монорепозиторий, три уровня пакетов

```
jobdesk/            домен (без изменений): hh.client/mapper/dedup/grades, models.Vacancy
jobdesk_platform/   инфраструктура без домена: Database, tracing, rq-раннер, liveness,
                    healthcheck, recover-цикл, SSE-хелперы, pubsub-хаб, AppError,
                    сервисный токен (dependency) и ServiceClient
backend/            ядро
jobdesk_hh/         HH-сервис
```

Правила импорта (тест `tests/test_import_boundaries.py`, разбор AST):
`jobdesk` → ничего из остальных; `jobdesk_platform` → только `jobdesk` (домен ниже
инфраструктуры: обработчику `CONFIG_INVALID` нужен `jobdesk.config.loader`);
`jobdesk_hh` → `jobdesk`, `jobdesk_platform`; `backend` → всё, кроме `jobdesk_hh`.

В `jobdesk_platform` переезжает то, что уже не зависит от доменных настроек ядра:
`workers/liveness.py`, `healthcheck.py`, `recover_loop.py`, `api/sse.py`,
`services/pubsub_hub.py`, `errors.py`; `tracing.py` и `workers/runner.py` получают параметры
вместо `backend.settings`. Старые модули ядра остаются алиасами (`sys.modules[__name__] =`
модуль платформы): десятки импортов не меняются, а монкипатч в тестах попадает в тот же объект.

*Альтернатива:* отдельные репозитории — дороже на этапе, когда контракты ещё меняются;
разрешить сервису импортировать `backend` — граница остаётся на словах.

### D2. Схема `hh`, перенос таблиц миграцией ядра

- Миграция ядра `054_hh_schema`: `CREATE SCHEMA IF NOT EXISTS hh`; для `hh_listings` и
  `hh_sync_jobs`: если есть только `public.X` — `ALTER TABLE … SET SCHEMA hh` (индексы и
  данные едут с таблицей); если есть обе — `INSERT INTO hh.X SELECT … ON CONFLICT DO NOTHING`
  и `DROP TABLE public.X`; затем снимается FK `requested_by`.
- Цепочка HH (`jobdesk_hh/migrations`, `version_table_schema="hh"`, `hh.alembic_version`):
  `hh_0001_baseline` создаёт схему и таблицы `IF NOT EXISTS` ровно в виде, который
  оставляет 054. Применяет `jobdesk_hh` при старте под своим advisory lock (≠ ключу ядра).
- Порядок первого старта неважен: ядро раньше — 054 переносит, baseline ничего не делает;
  HH раньше — baseline создаёт пустые таблицы, потом 054 сливает в них строки из `public`
  и удаляет старые. Это сценарии спеки `platform/internal-services`.
- Модели HH — отдельный `MetaData` в `jobdesk_hh/db/models.py` со `schema="hh"`; из
  `backend/db/models.py` строки HH удаляются.

*Альтернатива:* миграция переноса в цепочке HH — тогда старые ревизии ядра на пустой
базе снова создают `public.hh_*`, и их некому убрать.

### D3. Внутренний API ядра

- Роутер `backend/api/routes/internal.py` с префиксом `/internal/v1` на том же приложении
  и порту, вне `/api/v1`. Vite и внешний reverse-proxy проксируют только `/api`.
  Dependency `require_service_token` (из платформы) сравнивает Bearer с
  `CORE_INTERNAL_TOKEN` через `hmac.compare_digest`; пользовательская auth тут не участвует.
- `POST /internal/v1/vacancies/lookup` `{source_post_keys}` → `{vacancies: [Vacancy]}`.
- `POST /internal/v1/sources/hh/vacancies` `{vacancies: [Vacancy]}` →
  `{inserted, updated_existing}`; тело — та же логика, что у `upsert_hh_vacancies`
  (переезжает в `vacancy_repository`), одной транзакцией.
- Тело — pydantic-модель `jobdesk.models.vacancy.Vacancy` как есть: она уже является
  контрактом между ядром и доменом. Версионирование — префиксом `/v1`.
- Токены: `CORE_INTERNAL_TOKEN` (принимает ядро) и `HH_SERVICE_TOKEN` (принимает HH-сервис);
  у каждой стороны есть токен, которым она вызывает другую. В `dev` по умолчанию —
  фиксированные dev-значения через `_apply_app_env` (по правилу проекта); в `prod` —
  обязательны, ≥ 32 символов.

*Альтернатива:* отдельный порт для внутреннего API — второй uvicorn-сервер в каждом из
`API_WORKERS` процессов. Хватает токена и того, что `/internal` не проксируется наружу.

### D4. Чтение вакансий HH-сервисом — пачками по группам

Сейчас `get_vacancy_by_source_post_key` зовётся на каждую группу и каждый дубль. Вместо
этого перед обработкой очередного окна групп (пока ключей канонов и дублей ≤ 500) HH-сервис
одним `lookup` получает их вакансии. Семантика та же: каждый hh id входит ровно в одну
группу sync, а писать вакансии HH, кроме этого sync, некому (единственный активный sync).
Так 2000 групп повторного sync — около десяти внутренних запросов, а не 2000+.

### D5. Клиент между сервисами

`jobdesk_platform.service_client.ServiceClient` поверх `httpx.Client`: `base_url`, Bearer,
тайм-аут, 3 повтора с backoff на сетевые ошибки и 502/503/504 (все внутренние операции
идемпотентны), разбор `detail` в `UpstreamError(status, detail)`, сеть и не-JSON —
`UpstreamUnavailable`. httpx уже инструментирован OTel, `traceparent` идёт автоматически.
Для тестов клиент принимает готовый `httpx.Client` — туда подставляется `TestClient`
приложения другой стороны, и тест проходит путь «ядро → сервис → ядро» в одном процессе.

### D6. Прокси `/api/v1/hh/*` в ядре

`backend/api/routes/hh.py` сохраняет пути, `response_model`, `require_admin`,
`verify_api_key`, idempotency (`begin` → прокси → `complete`, ошибка сервиса → `abandon`).
Тела ответов сервиса отдаются как есть; ядро передаёт `X-JobDesk-User-Id`. SSE: ядро
аутентифицирует тикет и админа, открывает поток к сервису `httpx.AsyncClient.stream`,
статус ≠ 200 до первого байта превращает в обычный ответ с ошибкой, иначе пересылает байты.
`UpstreamUnavailable` → 503 `UPSTREAM_UNAVAILABLE`, `retryable: true`.

### D7. Процессы HH-сервиса

- `jobdesk-hh-service`: FastAPI (uvicorn, 1 процесс), порт 8100, `/metrics` на 9105.
  API `/v1/status`, `/v1/sync`, `/v1/sync-jobs`, `/v1/sync-jobs/{id}`, `.../cancel`,
  `.../events`, `/health`. Lifespan: миграции схемы `hh`; в `inprocess` — recover и запуск
  джоб потоками; раз в час и при старте — purge listings и старых sync (под
  `pg_try_advisory_lock`, чтобы реплик могло быть несколько).
- `jobdesk-hh-worker`: RQ-воркер очереди `hh` на образе сервиса — как сейчас, recover в нём.
- `HH_SYNC_JOB_BACKEND` и прочие `HH_*` читает `jobdesk_hh/settings.py`; общий `config.yaml`
  (секции `hh`, `retention`) монтируется так же.
- Бэкфилл грейдов HH (`backfill_hh_grades_from_experience`) работает по `vacancies` и
  остаётся в очистке лидера ядра (переезжает в `vacancy_repository`).

### D8. Наблюдаемость

Счётчики `hh_*` и `record_*` переезжают в `jobdesk_hh/observability.py` с прежними
именами. Гейджи `db_hh_listings` и серии `kind="hh_sync"` у гейджей задач собирает
коллектор HH-сервиса; ядро их больше не отдаёт. Prometheus получает цель
`jobdesk-hh-service`; правило `up{job=~…}` и Loki-панели HH — новую цель/контейнер. Имена
метрик не меняются, поэтому панели Grafana, агрегирующие по имени, продолжают работать.

### D9. Docker

`Dockerfile.backend` — общий слой зависимостей и две цели: `backend` (jobdesk,
jobdesk_platform, backend, alembic) и `hh` (jobdesk, jobdesk_platform, jobdesk_hh). Compose:
`backend` получает `HH_SERVICE_URL=http://hh-service:8100`; `hh-service` и `hh-worker` —
образ `hh`, `CORE_INTERNAL_URL=http://backend:8000`, переменные `HH_*`. Прямой зависимости
`depends_on` между ядром и HH нет: каждая сторона переживает недоступность другой.

## Risks / Trade-offs

- [Двухфазная запись: ядро записало пачку, ответ потерян] → повтор той же пачки идемпотентен
  (upsert по `source_post_key`), `ServiceClient` повторяет; спека фиксирует отсутствие дублей.
- [Ядро недоступно во время sync] → sync падает `failed` после повторов, записанное остаётся;
  `listing_detail_fetched` ставится только после успешной записи пачки, иначе следующий sync
  пропустил бы детали, которых нет в базе. Это меняет порядок относительно текущего кода:
  `mark_detail_fetched` переносится на момент после подтверждённого flush.
- [Реэкспорт-шимы в `backend` расползутся] → задача в tasks на перевод импортов и тест,
  что новые модули ядра импортируют платформу напрямую, — в следующих этапах.
- [Миграция 054 на большой `hh_listings`] → `SET SCHEMA` — только метаданные, без
  перезаписи; слияние `INSERT … SELECT` бывает только на пустой базе.
- [Прокси SSE держит соединение в ядре] → async-клиент без потока threadpool; heartbeat
  сервиса проходит насквозь.
- [Тесты: два приложения в одном процессе] → `TestClient` потокобезопасен на уровне
  запросов; inprocess-поток sync пишет в ядро через тот же `TestClient`.

## Migration Plan

1. Деплой новой версии: `backend` применяет 054 (перенос), `hh-service` применяет baseline
   (no-op). Порядок контейнеров не важен (D2).
2. `.env`: `HH_API_TOKEN`, `HH_CONTACT_EMAIL` остаются в общем `.env` — compose отдаёт их
   сервису; в prod задать `CORE_INTERNAL_TOKEN`, `HH_SERVICE_TOKEN`.
3. Откат: downgrade 054 возвращает таблицы в `public` (без FK) — старый код читает их как
   раньше; `hh.alembic_version` остаётся и не мешает.

## Дальнейшие этапы (вне этого изменения)

**Этап 2 — LLM-шлюз `jobdesk_llm_gateway`.** OpenAI-совместимые `/v1/chat/completions` и
`/v1/embeddings`; ключ OpenRouter только в шлюзе; общий темп (переезжает Redis-гейт),
повторы на 429/транзиентные с `Retry-After`, исчерпание провайдера — код ошибки, по которому
клиент ставит `LlmQuotaExhausted`; метрики токенов и стоимости по модели и вызывающему
(каталог `llm_models.yaml`). Вызывающим `OpenRouterClient` меняет `base_url` и ключ
(сервисный токен), локальный лимитер и повторы в этом режиме выключены; прямой режим
остаётся для CLI и evals. Промпты, версии, fallback-модели, разбор, AI-кэш и квоты
пользователя остаются у вызывающих. Меняется `platform/llm-governance`.

**Этап 3 — Telegram-сервис `jobdesk_telegram`.** Схема `telegram`: `parse_runs`,
`telegram_channel_checkpoints`, `channel_candidates`, `telegram_forwards`; сессия Telethon и
`jobdesk login` — только тут. `run_parse` (правила, AI через шлюз, дедуп, метрики) в сервисе;
вакансии — `POST /internal/v1/sources/telegram/vacancies` с `parse_run_id` (ядро пишет
`parse_run_vacancies` и `vacancy_sightings`, FK на `parse_runs` снимаются); корпус для дедупа,
курсы и AI-кэш — внутренние эндпоинты ядра (AI-кэш — HTTP-адаптер порта
`AIExtractionCacheBackend`). Ядро проксирует `/runs/*` и `/channels/*`, снимок конфигурации
собирает ядро (`build_config`, whitelist `RunCreateRequest`). Главная сложность — финализация:
сейчас статус прогона и остаток вакансий пишутся одной транзакцией, после выноса — «вакансии
в ядро, затем финализация в сервисе», с идемпотентным повтором.
