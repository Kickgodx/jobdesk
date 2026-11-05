# Tasks

## 1. Пакет jobdesk_platform и граница импортов

- [x] 1.1 Создать `jobdesk_platform/` и перенести туда `liveness`, `healthcheck`, `recover_loop`, `api/sse.py`, `pubsub_hub`, `errors.py` (AppError, коды); старые модули `backend` оставить реэкспортами. Проверка: `pytest -n 8 -q` зелёный без правок тестов.
- [x] 1.2 Параметризовать `tracing.setup_tracing` и `workers/runner.run_rq_worker` (имя сервиса, настройки — аргументами, без `backend.settings`) и перенести в платформу. Проверка: `tests/test_tracing.py`, `tests/test_rq_worker_entrypoints.py` зелёные.
- [x] 1.3 Добавить `jobdesk_platform.db.Database` (engine + sessionmaker + `session_scope`, JSON-сериализатор как у ядра) и `default_database_url()` (127.0.0.1 + `POSTGRES_PASSWORD`). Проверка: юнит-тест на сессию против `pg_db`.
- [x] 1.4 Добавить `require_service_token` (FastAPI dependency, `hmac.compare_digest`, 401 `NOT_AUTHENTICATED`) и `ServiceClient` (Bearer, тайм-аут, 3 повтора на сеть/502/503/504, `UpstreamError`/`UpstreamUnavailable`, принимает готовый `httpx.Client`). Проверка: юнит-тесты с `httpx.MockTransport` — повторы, разбор `detail`, не-JSON → `UpstreamUnavailable`.
- [x] 1.5 Тест `tests/test_import_boundaries.py` (AST): правила из design D1. Проверка: тест зелёный; временный запрещённый импорт его роняет.

## 2. Внутренний API ядра для вакансий

- [x] 2.1 Настройки ядра: `CORE_INTERNAL_TOKEN`, `HH_SERVICE_URL`, `HH_SERVICE_TOKEN`, `HH_SERVICE_TIMEOUT_SECONDS`; dev-значения токенов и проверка длины в prod — в `_apply_app_env`. Проверка: тесты в `tests/test_settings_env.py` (prod без токена — ошибка называет переменную; dev — значение по умолчанию).
- [x] 2.2 Перенести `upsert_hh_vacancies`, `get_vacancy_by_source_post_key` (→ пачкой `get_vacancies_by_source_post_keys`) и `backfill_hh_grades_from_experience` из `hh_repository` в `vacancy_repository`; лидер ядра продолжает бэкфилл. Проверка: `tests/test_hh_grades_backfill.py`, `tests/test_upsert_vacancies.py` на новых импортах.
- [x] 2.3 Роутер `backend/api/routes/internal.py` (`/internal/v1`): `POST /vacancies/lookup` (1–500 ключей), `POST /sources/hh/vacancies` (1–500, одна транзакция, FX). Проверка: `tests/test_internal_api.py` — все сценарии `platform/internal-services` про токен, lookup и запись пачки (включая повтор и невалидную вакансию).

## 3. Схема hh и миграции

- [x] 3.1 Миграция ядра `054_hh_schema`: схема `hh`, перенос или слияние `hh_listings`/`hh_sync_jobs`, снятие `fk_hh_sync_jobs_requested_by`; downgrade возвращает в `public`. Модели HH в `backend/db/models.py` до переноса кода указывают на схему `hh` без FK (удаляются в 4.2). Проверка: тест миграции с `pg_empty_db` — строки HH до 053 доступны в `hh.*` после head.
- [x] 3.2 `jobdesk_hh/db/models.py` (`schema="hh"`, свой `MetaData`) и цепочка `jobdesk_hh/migrations` (`version_table_schema="hh"`, baseline `IF NOT EXISTS`, расширение `version_num`), `jobdesk_hh/migrate.py` под своим advisory lock. Проверка: тест «сервис раньше ядра на пустой базе» — одна копия таблиц, в `hh`; тест «ядро раньше» — baseline no-op.
- [x] 3.3 `tests/conftest.py`: `pg_database` применяет обе цепочки, список TRUNCATE включает таблицы схемы `hh` (квалифицированные имена). Проверка: `pytest -n 8 -q` зелёный.

## 4. HH-сервис: домен и джобы

- [x] 4.1 `jobdesk_hh/settings.py`: `HH_API_TOKEN`, `HH_CONTACT_EMAIL`, `HH_SYNC_JOB_BACKEND` (+ lease/stuck/attempts/rq timeout), `HH_SERVICE_TOKEN`, `CORE_INTERNAL_URL`, `CORE_INTERNAL_TOKEN`, порты, `APP_ENV`; убрать `HH_*` из `backend/settings.py`. Проверка: тест — `HH_SYNC_JOB_BACKEND=redis` останавливает старт с именем переменной.
- [x] 4.2 Перенести репозитории listings и джоб (`hh_repository` без вакансий, `hh_sync_job_repository`), `HhSyncDbLock` в `jobdesk_hh/db/`; удалить модели HH из `backend/db/models.py`. Проверка: `tests/test_hh_sync_lock.py`, `tests/test_hh_listings_ttl.py` перенесены и зелёные.
- [x] 4.3 `jobdesk_hh/core_client.py` поверх `ServiceClient`: `lookup(keys)` чанками ≤ 500, `upsert_hh(vacancies)`. Перенести `hh_sync_service` → `jobdesk_hh/sync.py`: lookup окнами групп (design D4), запись пачек через ядро, `mark_detail_fetched` — после подтверждённого flush. Проверка: `tests/test_hh_batch_flush.py`, `tests/test_hh_review_fixes.py` перенесены и зелёные; новый тест: ядро отвечает 503 при flush → sync `failed`, ранние пачки в базе, детали неподтверждённой пачки не отмечены.
- [x] 4.4 Перенести `hh_sync_window`, `hh_sync_events`, `hh_sync_job_service`, `hh_sync_tasks`, `recover_hh_sync` в `jobdesk_hh`; `requested_by` — из заголовка, без FK. Проверка: `tests/test_hh_sync_jobs.py`, HH-часть `tests/test_job_cancel.py` перенесены и зелёные в `inprocess` и `rq` (fakeredis).
- [x] 4.5 `jobdesk_hh/maintenance.py`: purge listings и старых sync при старте и раз в час под `pg_try_advisory_lock`; убрать HH из `db_cleanup.py` и `main.py` ядра. Проверка: тест «старый HH sync» и TTL listings; тест `test_db_cleanup.py` без HH.

## 5. HH-сервис: API, процессы, метрики

- [x] 5.1 `jobdesk_hh/api.py`: `/health`, `/v1/status`, `/v1/sync`, `/v1/sync-jobs[/{id}[/cancel|/events]]` под `require_service_token`; lifespan — миграции, recover и запуск в `inprocess`, maintenance. Entrypoints `jobdesk-hh-service`, `jobdesk-hh-worker` в `pyproject.toml`. Проверка: тесты сервиса напрямую — health `degraded` без БД, 401 без токена, 422/503 окна и конфигурации.
- [x] 5.2 `jobdesk_hh/observability.py`: счётчики `hh_*`, коллектор `db_hh_listings` и `kind="hh_sync"` гейджей задач, `/metrics` на `HH_SERVICE_METRICS_PORT` (9105); убрать HH из `backend/observability.py`. Проверка: тест метрик сервиса; тест метрик ядра без `db_hh_listings`.

## 6. Прокси /api/v1/hh/* в ядре

- [x] 6.1 `backend/services/hh_service_client.py` и переписать `backend/api/routes/hh.py` в прокси: auth и idempotency в ядре, `X-JobDesk-User-Id`, тела как есть, `UpstreamUnavailable` → 503 `UPSTREAM_UNAVAILABLE`. Проверка: `tests/test_hh_api.py` проходит без изменений контракта через связку ядро → `TestClient(hh) → TestClient(core)`; новые тесты — 503 при пустом `HH_SERVICE_URL` и при недоступном сервисе, `/health` ядра `ok`.
- [x] 6.2 Прокси SSE `/hh/sync-jobs/{id}/events` через `httpx.AsyncClient.stream`: тикет и админ в ядре, ошибка до потока — обычный ответ. Проверка: тест SSE до `cancelled`/`completed` через прокси; 404 неизвестного sync.
- [x] 6.3 Трасса через границу: проверка `otel_exporter` — спаны ядра, сервиса и `hh_sync_job` в одной трассе. Проверка: тест в `tests/test_tracing.py`.

## 7. Docker, наблюдаемость, документация

- [ ] 7.1 `Dockerfile.backend`: общий слой зависимостей, цели `backend` и `hh`; compose — `hh-service`, `hh-worker` на цели `hh`, `HH_SERVICE_URL`, `CORE_INTERNAL_URL`, токены, healthcheck. Проверка: `docker compose build` и `docker compose up`, sync из UI доходит до `completed`.
- [x] 7.2 Prometheus: цель `jobdesk-hh-service`; правило `up{job=~…}`; Loki-панели HH читают и `hh-service`. Проверка: `bash scripts/check_alerts.sh`.
- [x] 7.3 Документация: CLAUDE.md (архитектура: пакеты, граница, внутренний API, команды запуска сервиса локально), README, `.env.example`, `docs/architecture/` (схема потоков HH), `config.md` при необходимости. Проверка: перечисленные команды запускаются как написано.

## 8. Интеграционная проверка

- [x] 8.1 Полный набор: `pytest -n 8 -q`, `ruff check .`, `cd frontend && npm run lint && npm run build`; CI workflow — шаги для новых пакетов (coverage source, проверка границы). Проверка: все команды зелёные.
- [x] 8.2 Ручная проверка на копии БД (`tgjobs_preview`): миграция 054 на реальных данных, sync `7d` до `completed`, отмена `running`, история sync сохранилась. Проверка: результаты записаны в отчёт изменения.

Отчёт 8.2 (2026-11-04, копия рабочей базы; ядро :8010 и HH-сервис :8110 запущены локально, `inprocess`, без Redis):

- До: ревизия ядра 053, `public.hh_listings` 776 (709 с деталями), `public.hh_sync_jobs` 10, HH-вакансий 776.
- Порядок «сервис раньше ядра»: baseline создал пустые `hh.*`, затем 054 слил строки и удалил `public.*`.
  После: одна копия таблиц в `hh`, 776 / 709 и 10 sync, FK `fk_hh_sync_jobs_requested_by` снят.
- `GET /api/v1/hh/status` и `/hh/sync-jobs` через прокси — прежние счётчики и история.
- Sync `7d`: `completed` за 6,3 с — найдено 156, групп 150, дублей 6, детали 4 / пропущено 146,
  вакансий +1 / ~155, 6 пачек записи, один `lookup` на все группы, ошибок нет; `requested_by` — id
  администратора. Повтор с тем же `Idempotency-Key` — тот же `job_id`; второй sync без ключа — 409.
- Sync `all` с отменой в `running`: `cancelled`, остановлен на 4-й странице поиска, детали не
  запрашивались, purge пропущен.
- HH-сервис остановлен: `/api/v1/hh/status` — 503 `UPSTREAM_UNAVAILABLE` (`retryable`), `/health` и
  список вакансий ядра — 200.
- Замечание вне изменения: при локальном запуске на Windows с выводом в файл логгер не пишет
  символ «→» в cp1251 (`jobdesk.hh.client`, «Logging error»); в Docker вывод в UTF-8.
- Копия удалена; рабочая база осталась на 053 без схемы `hh`.

## Workflow follow-up

- Мерж `feature/hh-service` в `master` через `--no-ff` после ревью.
- Архивировать изменение (`openspec archive hh-service`), спеки синхронизировать.
- Следующее изменение — `llm-gateway` (этап 2 из design.md).
