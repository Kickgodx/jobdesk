# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Правила проекта (проверка изменений, LLM hygiene, миграции, зависимости, ветки) —
в `AGENTS.md`, он подключён ниже и здесь не дублируется.

@AGENTS.md

## Команды

Окружение — Windows, `.venv` в корне. Полный набор проверок — в `AGENTS.md`; дополнительно:

```bash
# один файл / один тест / по подстроке
.venv/Scripts/python.exe -m pytest tests/test_llm_jobs.py -q
.venv/Scripts/python.exe -m pytest tests/test_llm_jobs.py::test_name -q
.venv/Scripts/python.exe -m pytest -k "lease and recover" -q

# весь стек (postgres, redis, backend, parse-worker, llm-worker, hh-service, hh-worker, frontend)
docker compose up --build                 # UI :5173, API :9000/docs
# observability (Prometheus/Grafana/Loki/Alloy/Tempo) — только при COMPOSE_PROFILES=observability в .env

# backend локально без Docker и без Redis
# env: PARSE_JOB_BACKEND=inprocess LLM_JOB_BACKEND=inprocess
# DATABASE_URL не нужен: без него — tgjobs:<POSTGRES_PASSWORD из .env>@127.0.0.1:5432/tgjobs
# (127.0.0.1, не localhost: порт опубликован только на IPv4, через ::1 psycopg на Windows ждёт connect timeout)
.venv/Scripts/python.exe -m uvicorn backend.main:app --reload --port 8000
# (всегда один процесс; несколько — API_WORKERS у jobdesk-api, нужны Redis, JWT_SECRET и rq)

# HH-сервис локально (вкладка HH): ядру HH_SERVICE_URL=http://127.0.0.1:8100, сервису
# HH_SYNC_JOB_BACKEND=inprocess CORE_INTERNAL_URL=http://127.0.0.1:8000; токены в dev — общие по умолчанию
.venv/Scripts/jobdesk-hh-service.exe

# frontend локально: /api проксируется Vite (VITE_API_PROXY_TARGET=http://localhost:9000 для
# compose-backend, по умолчанию :8000 — uvicorn); same-origin нужен refresh-cookie SameSite=Strict
cd frontend && npm run dev

# CLI: первая авторизация Telethon, дамп/импорт БД
.venv/Scripts/python.exe -m jobdesk.cli login --config config.yaml
.venv/Scripts/python.exe -m jobdesk.cli db dump -o backup/jobdesk_dump.json
```

У frontend тестов нет — проверка это `npm run lint` + `npm run build` (tsc).
В `addopts` pytest стоит `-m "not llm"`: live-тесты нужно включать явно (`-m llm`).

## Архитектура

Python-пакеты в одном репозитории плюс React UI:

- **`jobdesk/`** — ядро: Telethon-fetch, правиловое извлечение, AI-enrichment,
  дедуп, FX, HH-клиент, экспорт. Не знает о FastAPI/SQLAlchemy.
- **`backend/`** — ядро: FastAPI, SQLAlchemy-репозитории, очереди RQ, воркеры, auth, observability.
- **`jobdesk_platform/`** — инфраструктура процессов без домена: `Database`, tracing, RQ-раннер,
  liveness/healthcheck, recover-цикл, SSE, pub/sub, `AppError`, сервисные токены, `ServiceClient`.
  Старые модули `backend` (`errors`, `api/sse`, `workers/liveness` …) — алиасы `sys.modules` на него.
- **`jobdesk_hh/`** — HH-сервис (вынесен из ядра, этап 1 из 3 — `openspec/changes/hh-service`):
  sync, кэш выдачи, джобы, API `/v1/*` (вызывает только ядро, `HH_SERVICE_TOKEN`), entrypoints
  `jobdesk-hh-service` / `jobdesk-hh-worker`. Данные — схема `hh` (своя цепочка Alembic в
  `jobdesk_hh/migrations`, применяет сам сервис). Вакансии — только через внутренний API ядра.
- **`jobdesk_mcp/`** — MCP-сервер (stdio) для Claude: только клиент HTTP API с персональным
  токеном (`tgj_…`), не импортирует ни `backend`, ни ядро. Зависимость `mcp` — extra `mcp`,
  в образ backend не входит. Новый инструмент = эндпоинт API; мутирующий эндпоинт, который
  должен быть доступен токену, помечается `@pat_scope("llm")` (`backend/api/deps.py`), иначе PAT
  получает на нём 403.
- **`frontend/`** — Vite + React + TanStack Query; весь HTTP — в `src/api/client.ts`,
  типы ответов — в `src/types/api.ts` (держать в синхроне с `backend/schemas.py`).

Схемы потоков (mermaid) — `docs/architecture/`.

### Границы пакетов и сервисов

Кто кого импортирует, проверяет `tests/test_import_boundaries.py`: `jobdesk` — никого
(кроме `jobdesk/cli/db.py`); `jobdesk_platform` — только `jobdesk`; `jobdesk_hh` — `jobdesk` и
платформу, не `backend`; `backend` — не `jobdesk_hh` (только HTTP).

- **Внутренний API ядра** — `/internal/v1/*` (`backend/api/routes/internal.py`), вне `/api/v1`,
  только с `CORE_INTERNAL_TOKEN`: чтение вакансий по `source_post_key` и запись пачек HH.
  Контракт — `jobdesk/contracts/core_internal.py`. Сервису нужны данные ядра — новый эндпоинт здесь.
- **Прокси** — публичные `/api/v1/hh/*` остаются в ядре (`routes/hh.py`): admin, idempotency и
  SSE-тикет проверяет ядро, тело/ошибку сервиса отдаёт как есть; сервис недоступен — 503
  `UPSTREAM_UNAVAILABLE`. Контракт — `jobdesk/contracts/hh_service.py`.
- **Тесты** — ядро и HH-сервис в одном процессе: `hh_client` / `hh_service_stack` в conftest,
  после `monkeypatch.setenv` — `clear_settings_caches()` (кэш настроек обоих).

### Граница core ↔ backend

Зависимость односторонняя: `backend` импортирует `jobdesk`, обратно — нет
(единственное исключение — `jobdesk/cli/db.py`, дамп/импорт БД).
Там, где ядру нужна инфраструктура, используется порт: Postgres-кэш LLM
(`AIExtractionCacheBackend` в `jobdesk/ai/cache.py`, адаптер `DbAIExtractionCache`)
и общий лимит запросов к LLM (`GlobalRateGate` в `jobdesk/ai/rate_limit.py`,
адаптер на Redis). Оба регистрирует `backend.core_ports.register_core_ports()` при старте
каждого процесса с LLM: FastAPI lifespan, `parse-worker`, `llm-worker`. Новый entrypoint с
LLM обязан вызвать её — без кэша `AIEnrichmentService` не создаётся, и LLM-разбор молча
уходит в правила. `tests/conftest.py` регистрирует кэш на всю сессию, поэтому тест
entrypoint'а сбрасывает регистрацию сам (см. `tests/test_rq_worker_entrypoints.py`).

### Parse pipeline

`POST /runs` → `backend/services/run_service.py` → строка `parse_runs` (`queued`,
со снимком конфигурации) → RQ-очередь `parse` или поток (`PARSE_JOB_BACKEND`) →
`backend/workers/parse_tasks.run_parse_job` → `jobdesk/parse_runner.run_parse`:

Telethon fetch → `ingestion.process` (rules → опциональный AI → policy gates) →
dedup → FX в ₽ → UPSERT в `vacancies` → метрики.

- Одновременно допустим один активный прогон (409 `CONFLICT_ACTIVE_RUN`).
- Конфиг прогона = `config.yaml` + overrides из тела запроса
  (`backend/config_builder.build_config`); воркер читает **снимок** из
  `parse_runs.config_snapshot`, а не файл. Overrides — whitelist-схема
  `RunCreateRequest` (`extra="forbid"`): новое поле добавлять явно и только
  если его безопасно отдать клиенту (не `base_url`, не пути, не лимиты).
- `jobdesk/ingestion/` — общий путь для Telegram и ручного импорта
  (`POST /vacancies/import`, `MANUAL_POLICY` — без пост-фильтров и порога score).
  HH sync идёт мимо него: `hh/mapper` + `hh/dedup`, без LLM.
- `id` вакансии — content-hash очищенного текста (`utils/hashing.vacancy_content_id`),
  для HH — `sha256(hh:{id})`; UPSERT по нему и есть межпрогонный дедуп.

### Durable-джобы (parse, LLM, HH sync)

HH sync устроен так же, но живёт в HH-сервисе: таблица `hh.hh_sync_jobs`, recover в `hh-worker`
или (в `inprocess`) в процессе `jobdesk-hh-service`, а не в API ядра.

Источник истины по статусу — Postgres, Redis — только транспорт (очередь RQ +
pub/sub для SSE). Все три типа джоб устроены одинаково:

- claim с lease (`lease_owner`, heartbeat) → `running`; истёкший lease или
  `queued` без живой RQ-джобы поднимает recover-цикл (`backend/workers/recover*.py`):
  в режиме `rq` он живёт в воркерах, в `inprocess` — в lifespan API;
- каждый claim увеличивает `attempts`; исчерпав `PARSE_JOB_MAX_ATTEMPTS` /
  `LLM_JOB_MAX_ATTEMPTS`, зависшая джоба уходит в `failed` / `error`, а не в requeue;
- отмена — флаг `cancel_requested` в БД у всех трёх типов (переживает рестарт API):
  `queued` сразу становится `cancelled`, у `running` воркер проверяет флаг (прогон —
  поток-поллер, импорт — перед каждым текстом, HH sync — перед страницей поиска и
  группой деталей, мэтч/письмо — после ответа модели, результат отбрасывается);
  зависший `running` с флагом recover закрывает `cancelled`, а не ставит на повтор;
- прогресс прогона — колонка `parse_runs.progress`; SSE (`/runs/{id}/events`,
  `/llm-jobs/{id}/events`) читает Redis pub/sub с fallback на poll БД;
- матчинг и письма: кэш в БД → 200, иначе `llm_jobs` + 202 `{job_id}`; ручной импорт
  с LLM и переразбор сохранённых вакансий — тоже `llm_jobs` (`kind=import` / `reextract`,
  пакетные `BATCH_KINDS`: без резюме/вакансии, итог по элементам в `result`)
  (`backend/services/llm_job_service.py`, `backend/workers/llm_tasks.py`);
- мутации parse / HH sync / import принимают `Idempotency-Key`
  (`backend/services/idempotency.py`): резерв коммитится отдельной сессией до
  side-effect.

Бэкенды `inprocess` (дефолт в `Settings`, используется тестами) и `rq`
(Docker) должны оставаться взаимозаменяемыми — изменение в одном пути
проверять и во втором.

### Данные и доступ

- `backend/db/repository.py` — совместимый facade-реэкспорт; новый код
  импортирует bounded-репозитории напрямую (`parse_run_repository`,
  `vacancy_repository`, `user_content_repository`, `llm_job_repository`).
- Фильтры списка, метрик, аналитики и экспорта строятся одной функцией
  (`backend/db/vacancy_filters.py`) из `VacancyQuery` — новый фильтр добавлять там,
  иначе список и аналитика разойдутся.
- Специализация — роль, технология — стек: языки и инструменты из специализаций
  переносит `split_specializations` (`jobdesk/config/schema.py`) в каждой точке
  входа (правила, ответ LLM, HH, `FiltersConfig`, `VacancyQuery`); имена технологий —
  через `canonical_technologies`.
- Только Postgres (`DATABASE_URL` другого диалекта отклоняет `Settings`): FTS,
  JSONB, advisory lock, `ON CONFLICT` пишутся напрямую, без веток под диалекты.
  Тесты — тоже на Postgres (testcontainers или `TEST_DATABASE_URL`, см. `AGENTS.md`).
- Per-user (по `user_id`, JWT): резюме, пресеты, матчи, письма, `llm_jobs`.
  Общее: вакансии, аналитика, курсы, AI-кэш — чтение любому
  залогиненному (`get_current_user` на роутере), мутации — `require_admin`.
  Прогоны (`/runs/*`, включая чтение и SSE), ручной импорт и HH — целиком admin;
  на UI вкладки «Парсинг»/«Импорт»/«HH» закрыты `RequireAdmin` в `App.tsx`.
  Анонимно доступны только `/health` и `/auth/register|login` (`/auth/refresh|logout` —
  по refresh-cookie). Сессия — короткий access JWT в памяти UI + refresh-токен в
  httpOnly cookie (`POST /auth/refresh`, ротация). SSE получает доступ одноразовым
  `?ticket=` из `POST /auth/sse-ticket` (`authenticate_sse` в `backend/api/deps.py`);
  JWT в query не принимается.
- Ошибки API — `AppError` / `http_error` из `backend/errors.py` с
  `detail = {code, message, retryable}`; frontend разбирает это в `ApiError`.
- Итоговый `overall_score` матчинга считает backend детерминированно из
  breakdown LLM (веса и cap за critical-пробелы) — не доверять числу из ответа модели.

### Конфигурация

- `config.yaml` (схема — `jobdesk/config/schema.py`, описание — `config.md`) —
  доменные настройки; `backend/settings.py` — env-настройки процесса (`.env`).
  `get_settings()` под `lru_cache`: в тестах после `monkeypatch.setenv` нужен
  `get_settings.cache_clear()` (и `reset_engine()` при смене `DATABASE_URL`) —
  см. фикстуру `api_client`.
- `APP_ENV` (`dev` по умолчанию | `prod`) переключает только умолчания в
  `Settings._apply_app_env`: регистрация, тела в HTTP-логе, обязательность
  `JWT_SECRET`. Новое различие dev/prod добавлять туда же (поле `None` →
  значение по режиму), а не ветками `if is_production` по коду; тесты идут в `dev`.
- `llm_models.yaml` — каталог моделей и цен для UI и расчёта стоимости токенов.
