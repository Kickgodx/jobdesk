# Proposal

## Why

Источники вакансий и LLM живут в одной кодовой базе и одной схеме с ядром: воркеры уже
отдельные процессы, но пишут в `vacancies` напрямую, и вынести любой из них — значит
сначала развязать общий доступ к таблицам. HH sync — самый изолированный кандидат (своя
очередь, свои таблицы, без LLM), поэтому на нём закладывается общий способ выноса:
внутренний API ядра для вакансий, сервисные токены, проксирование публичных эндпоинтов
и собственная схема сервиса в той же базе. Это этап 1 из трёх; за ним — LLM-шлюз и
Telegram-сервис по тому же образцу.

## What Changes

- Новый сервис `jobdesk_hh` (пакет в репозитории, свой entrypoint и образ): поиск, детали,
  дедуп HH, кэш выдачи, durable-джобы HH sync (claim, lease, recover, отмена), поток событий.
  Сервис SHALL NOT импортировать `backend` и ходить в таблицы ядра.
- Таблицы `hh_listings` и `hh_sync_jobs` переезжают в схему `hh` той же базы (данные
  сохраняются); FK `hh_sync_jobs.requested_by → users` снимается — id пользователя
  передаёт ядро. У схемы `hh` своя цепочка миграций, её применяет HH-сервис при старте.
- Внутренний API ядра `/internal/v1/*` (вне `/api/v1`, только с сервисным токеном):
  чтение вакансий по `source_post_key` и запись пачки вакансий HH (с FX и прежней семантикой
  `upsert_hh_vacancies`). Это единственный путь HH-сервиса к `vacancies`.
- Публичные `/api/v1/hh/*` остаются в ядре и с тем же контрактом: ядро проверяет админа,
  `Idempotency-Key` и SSE-тикет, затем проксирует запрос в HH-сервис. Frontend и MCP не меняются.
  Недоступный HH-сервис → 503 `UPSTREAM_UNAVAILABLE`, `retryable: true`.
- Общий пакет `jobdesk_platform`: сессия БД, tracing, RQ-раннер, liveness, recover-цикл,
  сервисная аутентификация и HTTP-клиент между сервисами. Ядро и сервисы используют его.
- Настройки HH (`HH_API_TOKEN`, `HH_CONTACT_EMAIL`, `HH_SYNC_JOB_*`, `HH_WORKER_METRICS_PORT`)
  переезжают в HH-сервис. В ядре появляются `HH_SERVICE_URL`, `HH_SERVICE_TOKEN`,
  `CORE_INTERNAL_TOKEN`. **BREAKING** для `.env`/деплоя: переменные HH задаются сервису, а
  в compose появляется `hh-service` (+ `hh-worker` на его образе).
- Очистка кэша выдачи и старых HH sync — в HH-сервисе; бэкфилл грейдов HH по опыту остаётся
  в ядре (это данные `vacancies`).
- Метрики `hh_*` и `db_hh_listings` отдаёт HH-сервис под прежними именами; Prometheus
  получает новую цель.

Вне объёма: LLM-шлюз и Telegram-сервис (этапы 2–3, см. design.md), отдельная база на
сервис, изменения UI.

## Capabilities

### New Capabilities
- `platform/internal-services`: границы сервисов — внутренний API ядра для вакансий,
  сервисные токены, проксирование публичных эндпоинтов в сервис, собственная схема и
  миграции сервиса, запрет межсхемного доступа.

### Modified Capabilities
- `sources/hh-sync`: sync выполняет HH-сервис; очистка кэша выдачи — при старте HH-сервиса
  и раз в час; поведение при недоступном сервисе.
- `platform/data-maintenance`: схема сервиса мигрирует сам сервис под своей блокировкой;
  плановая очистка API больше не включает кэш выдачи HH, а срок хранения HH sync применяет
  HH-сервис.
- `platform/durable-jobs`: в `inprocess` восстановление HH sync живёт в процессе HH-сервиса,
  а не API.
- `platform/runtime-config`: `HH_SYNC_JOB_BACKEND` читает HH-сервис.
- `platform/alerting`: `db_jobs_lease_expired{kind="hh_sync"}` публикует HH-сервис; `TargetDown` и
  `WorkerMissing` следят и за `hh-service`.

## Impact

- Код: новый `jobdesk_hh/`, `jobdesk_platform/`; из `backend` уходят `services/hh_sync_*`,
  `db/hh_*`, `workers/hh_*`, `workers/recover_hh_sync.py`; `backend/api/routes/hh.py`
  становится прокси; новый `backend/api/routes/internal.py`; HH-части `observability.py`,
  `db_cleanup.py`, `main.py`.
- БД: миграция ядра 054 (перенос таблиц в схему `hh`, снятие FK), цепочка `jobdesk_hh`.
- Docker: `Dockerfile.backend` с целями `backend` и `hh`, сервисы `hh-service`, `hh-worker`
  в compose, цель Prometheus.
- Тесты: HH-тесты переезжают на сервис; фикстуры conftest мигрируют обе цепочки и чистят
  схему `hh`; тест границы импортов.
- Документация: README, CLAUDE.md (архитектура, команды), `.env.example`, `docs/architecture/`.
