# Архитектура по сервисам

Runtime-карта: Docker Compose, внешние API, observability.

```mermaid
flowchart LR
  subgraph UI
    FE["frontend<br/>:5173 · React"]
  end

  subgraph App
    BE["backend<br/>:9000→8000 · FastAPI<br/>API_WORKERS процессов, один — лидер"]
    PW["parse-worker<br/>RQ · :9102/metrics"]
    W["llm-worker<br/>RQ · :9101/metrics"]
    MW["maintenance-worker<br/>эмбеддинги · карта · обзор<br/>:9104/metrics"]
    CLI["CLI Typer<br/>login / db dump·import"]
  end

  subgraph HHS["HH-сервис (jobdesk_hh)"]
    HS["hh-service<br/>:8100 /v1 · :9105/metrics"]
    HW["hh-worker<br/>RQ · :9103/metrics"]
  end

  subgraph Data
    PG[("postgres<br/>:5432 · схемы public, hh")]
    RD[("redis<br/>:6379 · RQ")]
    PGB["pg-backup<br/>pg_dump + проверка"]
  end

  subgraph Obs
    PROM["prometheus<br/>:9090"]
    ALLOY["alloy<br/>:12345 · OTLP"]
    LOKI["loki<br/>:3100"]
    TEMPO["tempo<br/>:3200"]
    GRAF["grafana<br/>:3000"]
    AM["alertmanager<br/>:9093 · без доставки"]
  end

  subgraph Ext["Внешние"]
    TG["Telegram<br/>Telethon"]
    OR["OpenRouter"]
    HH["HH.ru API"]
    CBR["ЦБ FX"]
  end

  FE -->|"REST / SSE"| BE
  CLI --> PG
  CLI --> TG
  BE --> PG
  BE -->|enqueue job_id| RD
  RD --> PW
  RD --> W
  BE -->|"/api/v1/hh/* → /v1/* (HH_SERVICE_TOKEN)"| HS
  HS -->|"enqueue hh"| RD
  RD --> HW
  HS -->|"схема hh"| PG
  HW -->|"схема hh"| PG
  HW -->|"/internal/v1 (CORE_INTERNAL_TOKEN)"| BE
  PW --> PG
  W --> PG
  MW --> PG
  PW --> TG
  PW --> OR
  W --> OR
  HW --> HH
  MW -->|"эмбеддинги, LLM обзора"| OR
  BE -->|"лидер"| CBR
  BE -->|"/metrics :9100 (лидер)"| PROM
  PW -->|"/metrics :9102"| PROM
  W -->|"/metrics :9101"| PROM
  HS -->|"/metrics :9105"| PROM
  HW -->|"/metrics :9103"| PROM
  MW -->|"/metrics :9104"| PROM
  BE -->|"OTLP :4318"| ALLOY
  PW -->|"OTLP"| ALLOY
  W -->|"OTLP"| ALLOY
  HS -->|"OTLP"| ALLOY
  HW -->|"OTLP"| ALLOY
  MW -->|"OTLP"| ALLOY
  ALLOY --> LOKI
  ALLOY --> TEMPO
  PGB --> PG
  PGB -.->|"pg_backup.prom"| ALLOY
  ALLOY -->|"remote write"| PROM
  PROM -->|"алерты"| AM
  AM --> GRAF
  PROM --> GRAF
  LOKI --> GRAF
  TEMPO --> GRAF
```

| Сервис | Порт | Роль |
|--------|------|------|
| frontend | 5173 | Web UI |
| backend | 9000→8000; 9100 (только Docker-сеть) | API, JWT, постановка джоб в очереди; `API_WORKERS` процессов (по умолчанию 4). Курсы ЦБ, плановая очистка и `/metrics` на :9100 — только у лидера. `/api/v1/hh/*` проксирует в HH-сервис; `/internal/v1/*` — внутренний API вакансий для сервисов (сервисный токен) |
| parse-worker | 9102 (только Docker-сеть) | RQ `SimpleWorker`: прогоны парсинга (Telethon, LLM-разбор), очередь `parse`, concurrency=1 |
| llm-worker | 9101 (только Docker-сеть) | RQ `SimpleWorker`: мэтч/письма/ручной импорт с LLM; `/metrics` без DB COUNT-collector, с `db_pool_*` |
| hh-service | 8100, 9105 (только Docker-сеть) | HH-сервис (`jobdesk_hh`, образ из цели `hh` без кода ядра): API `/v1/*` для ядра, джобы HH sync, кэш выдачи и очистка; данные — схема `hh`, миграции своей цепочкой при старте |
| hh-worker | 9103 (только Docker-сеть) | RQ `SimpleWorker` HH-сервиса: фоновый HH sync (очередь `hh`); вакансии пишет через `/internal/v1` ядра |
| maintenance-worker | 9104 (только Docker-сеть) | Фоновое обслуживание каталога (`MAINTENANCE_BACKEND=worker`): индексация эмбеддингов, оценки зарплат, недельный обзор рынка, карта рынка и её пересчёт по кнопке. Без RQ — свой цикл; работает одна реплика (advisory lock) |
| redis | 6379 | Очереди RQ `llm` / `parse` / `hh` (AOF на томе `redis_data`) + pub/sub статуса SSE |
| postgres | 5432 | Вакансии, runs, users, FX, cache, `llm_jobs` (схема `public`, ядро); кэш выдачи и джобы HH (схема `hh`, HH-сервис) |
| pg-backup | — | `pg_dump` по расписанию с проверкой восстановления; состояние — файл `pg_backup.prom` в каталоге дампов |
| prometheus | 9090 | Scrape backend + воркеры (`dns_sd`); правила алертов (`observability/alerts/`); remote-write receiver для метрик k6 (`scripts/loadtest`) и бэкапа (Alloy) |
| alertmanager | 9093 | Сработавшие алерты Prometheus; доставки наружу нет — UI и Grafana → Alerting |
| grafana | 3000 | Дашборды / Explore (в т.ч. **DB pool**, **LLM jobs / queue**), Alerting |
| alloy | 12345 | Логи Docker + OTLP → Loki/Tempo; `pg_backup.prom` → Prometheus |
| loki | 3100 | Логи |
| tempo | 3200 | Трассы |

Параллелизм LLM: `docker compose up --scale llm-worker=N`. Локально без Redis: `LLM_JOB_BACKEND=inprocess`.

## Процессы API

- **`API_WORKERS`** — число процессов uvicorn в контейнере backend (GIL держит процесс на ~1 ядре).
  Пул соединений с Postgres делится между ними. Нескольким процессам нужны Redis (`rq`) и `JWT_SECRET`;
  локальный `uvicorn --reload` — всегда один процесс.
- **Лидер** (`backend/leader.py`) — процесс, удерживающий `pg_try_advisory_lock` на отдельном соединении.
  Только он синхронизирует курсы ЦБ при старте, раз в час чистит БД и поднимает сервер `/metrics`
  (сумма метрик всех процессов через `PROMETHEUS_MULTIPROC_DIR`). Если лидер упал, Postgres снимает
  lock вместе с сессией, и роль за ≤30 с берёт другой процесс. Обслуживание каталога (эмбеддинги, оценки
  зарплат, обзор и карта рынка) лидер выполняет только при `MAINTENANCE_BACKEND=inprocess` (локально);
  в compose его делает `maintenance-worker`, чтобы t-SNE и проходы индексации не занимали CPU и пул API.
- **Защита от перегрузки** (`backend/overload.py`) — у каждого процесса столько мест обработки, сколько
  соединений в пуле. Запрос, не получивший место за `API_QUEUE_TIMEOUT_SECONDS`, получает быстрый
  503 `OVERLOADED` (`Retry-After: 1`), а не висит до тайм-аута пула. `/health` идёт без очереди.
