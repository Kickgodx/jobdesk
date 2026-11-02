# platform/runtime-config Specification

## Purpose
Источники конфигурации и проверки при старте: доменный `config.yaml` и настройки
процесса из окружения, режимы `APP_ENV`, конфигурация для UI и проверка здоровья.

## Requirements

### Requirement: Конфигурация — config.yaml и окружение процесса
Доменные настройки MUST читаться из `config.yaml` (путь — `CONFIG_PATH`) вместе с каталогом `llm_models.yaml`, настройки процесса — из переменных окружения и `.env`. Правка `config.yaml` или `llm_models.yaml` SHALL подхватываться при следующем чтении без рестарта, изменение окружения — только после рестарта. Выполняемый прогон работает по снимку конфигурации (см. `parse-runs`).

#### Scenario: Правка порога в config.yaml
- **WHEN** `parser.vacancy_score_threshold` в `config.yaml` поменяли с 25 на 50
- **THEN** следующее чтение конфигурации даёт 50 без рестарта

#### Scenario: Изменение прочитанной конфигурации
- **WHEN** код изменил полученную конфигурацию
- **THEN** следующее чтение отдаёт значения из файла

### Requirement: Ошибка в config.yaml останавливает операцию с понятной причиной
Отсутствующий файл, некорректный YAML, корень не-объект, нарушение схемы и неизвестный ключ MUST давать ошибку загрузки конфигурации с причиной; ошибка о ключах перечисляет пути всех неизвестных ключей. Старые ключи `ai.models` и `ai.fallback_model` SHALL приниматься. API при ошибке отвечает 500 `CONFIG_INVALID` (см. `platform/api-conventions`). Ошибка SHALL NOT запоминаться: исправленный файл читается сразу. Снимок конфигурации прогона на неизвестные ключи не проверяется.

#### Scenario: Исправленный файл
- **WHEN** `config.yaml` содержал список вместо объекта, затем его исправили
- **THEN** первое чтение — ошибка загрузки, следующее — исправленная конфигурация

#### Scenario: Опечатка в ключе
- **WHEN** в `config.yaml` есть `fx.enabeld: false` и `parser.vacancy_score_treshold: 7`
- **THEN** конфигурация не загружается, ошибка называет `fx.enabeld` и `parser.vacancy_score_treshold`

#### Scenario: Старый ключ запасной модели
- **WHEN** в `config.yaml` задан `ai.fallback_model` вместо `ai.fallback_models`
- **THEN** конфигурация загружается, модель становится единственной запасной

#### Scenario: Конфигурация для UI при ошибке
- **WHEN** `config.yaml` не загружается, и запрошен `GET /config/defaults`
- **THEN** ответ 500 `CONFIG_INVALID` с `retryable: false`

### Requirement: Режим APP_ENV меняет только умолчания
`APP_ENV` MUST принимать `dev` (по умолчанию; также пусто, `development`, `local`) или `prod` (также `production`) без учёта регистра и пробелов; другое значение — ошибка старта. Режим SHALL менять только умолчания: в `dev` регистрация открыта и тела пишутся в HTTP-журнал; в `prod` регистрация закрыта, тела не пишутся, refresh-cookie получает `Secure`, а `JWT_SECRET` обязателен (см. `identity/authentication`). Явно заданная переменная побеждает в обоих режимах.

#### Scenario: Режим по умолчанию
- **WHEN** `APP_ENV` не задан
- **THEN** режим `dev`: регистрация открыта, тела пишутся в журнал

#### Scenario: Опечатка в режиме
- **WHEN** `APP_ENV=staging`
- **THEN** процесс не стартует

#### Scenario: Явное значение в prod
- **WHEN** `APP_ENV=prod` и `HTTP_LOG_BODIES=true`
- **THEN** тела пишутся в журнал

### Requirement: dev не запускается на внешнем адресе
В режиме `dev` API MUST отказываться стартовать, если адрес, на котором он доступен (`API_PUBLIC_ADDR`, иначе `API_HOST`), не loopback (`127.0.0.0/8`, `::1`, `localhost`). При `ALLOW_PUBLIC_DEV=true` SHALL стартовать с предупреждением. В `prod` адрес не ограничен.

#### Scenario: dev на всех интерфейсах
- **WHEN** `APP_ENV=dev`, `API_HOST=0.0.0.0`
- **THEN** старт прерывается с советом задать `APP_ENV=prod`

#### Scenario: Контейнер с публикацией на loopback
- **WHEN** `API_HOST=0.0.0.0`, `API_PUBLIC_ADDR=127.0.0.1`
- **THEN** API стартует

### Requirement: База данных — только PostgreSQL
`DATABASE_URL` MUST указывать на PostgreSQL, иначе процесс не стартует. Пустой `DATABASE_URL` SHALL означать Postgres из compose: `tgjobs@127.0.0.1:5432/tgjobs` с паролем `POSTGRES_PASSWORD` (URL-кодированным; без него — `tgjobs`).

#### Scenario: SQLite
- **WHEN** `DATABASE_URL=sqlite:///tgjobs.db`
- **THEN** процесс не стартует, ошибка говорит, что поддерживается только PostgreSQL

#### Scenario: Пароль со спецсимволами
- **WHEN** `DATABASE_URL` пуст, `POSTGRES_PASSWORD=p@ss/word`
- **THEN** используется `postgresql+psycopg://tgjobs:p%40ss%2Fword@127.0.0.1:5432/tgjobs`

### Requirement: Ошибка настроек не раскрывает значения
Ошибка проверки настроек процесса при старте MUST называть переменную и причину, но SHALL NOT содержать её значение.

#### Scenario: Короткий секрет
- **WHEN** в `prod` задан `JWT_SECRET=short-secret`
- **THEN** сообщение об ошибке не содержит `short-secret`

### Requirement: Конфигурация для UI
`GET /config` MUST отдавать доменную конфигурацию без каталога моделей. `GET /config/defaults` SHALL отдавать умолчания формы запуска: каналы, окно поиска (`week_ago` и `today`, если даты не заданы, с вычисленными датами и `max_days`), фильтры, пороги оценки, настройки AI, файл сессии Telegram и списки вариантов специализаций, форматов работы и грейдов. Доступ — по `identity/access-control`.

#### Scenario: Окно поиска не задано
- **WHEN** в `config.yaml` нет `search.date_from` и `search.date_to`
- **THEN** в умолчаниях `date_from` — `week_ago`, `date_to` — `today`, и обе вычисленные даты

### Requirement: Проверка здоровья
`GET /health` MUST быть доступен без входа и отвечать 200 с `status`: `ok`, если база отвечает и конфигурация загружается без ошибки, иначе `degraded`, а также флагами `database` и `config_loaded`. Причина ошибки конфигурации SHALL записываться в журнал и не попадать в ответ.

#### Scenario: База недоступна
- **WHEN** Postgres не отвечает
- **THEN** ответ 200, `status` — `degraded`, `database` — `false`

#### Scenario: Конфигурация не загружается
- **WHEN** файл `config.yaml` есть, но в нём неизвестный ключ
- **THEN** ответ 200, `status` — `degraded`, `config_loaded` — `false`, текста ошибки в ответе нет

### Requirement: Бэкенд задач — inprocess или rq
`PARSE_JOB_BACKEND`, `LLM_JOB_BACKEND` и `HH_SYNC_JOB_BACKEND` MUST принимать `inprocess` или `rq` без учёта регистра и пробелов; пустое значение или отсутствие переменной — `inprocess`. Другое значение SHALL быть ошибкой старта процесса, которая называет переменную.

#### Scenario: Опечатка в бэкенде
- **WHEN** `LLM_JOB_BACKEND=redis`
- **THEN** процесс не стартует, ошибка называет `LLM_JOB_BACKEND`

#### Scenario: Регистр и пробелы
- **WHEN** `PARSE_JOB_BACKEND=" RQ "`
- **THEN** прогоны идут через очередь RQ

### Requirement: Фоновое обслуживание каталога вне API
Индексацию эмбеддингов и оценки зарплат, недельный обзор рынка и карту рынка MUST выполнять один исполнитель на базу: при `MAINTENANCE_BACKEND=inprocess` (по умолчанию) — лидер API, каждую задачу в своём потоке; при `worker` — процесс `jobdesk-maintenance-worker`, а API SHALL NOT их выполнять. Реплик воркера может быть несколько: работает одна, держащая advisory lock; если она завершилась, роль берёт другая. `MAINTENANCE_BACKEND` принимает `inprocess` или `worker` без учёта регистра и пробелов; другое значение — ошибка старта, которая называет переменную.

#### Scenario: Отдельный воркер
- **WHEN** `MAINTENANCE_BACKEND=worker` у API и воркера
- **THEN** карту рынка и индексацию эмбеддингов выполняет воркер, лидер API — нет

#### Scenario: Две реплики воркера
- **WHEN** запущены две реплики `jobdesk-maintenance-worker`
- **THEN** задачи выполняет одна из них

### Requirement: Число процессов API
`API_WORKERS` MUST задавать число процессов API, принимающих запросы на одном порту: целое от 1, по умолчанию 1. Другое значение SHALL быть ошибкой старта, которая называет переменную. Каждый процесс применяет миграции под общей блокировкой (см. `platform/data-maintenance`) и отвечает на любой эндпоинт одинаково.

#### Scenario: По умолчанию один процесс
- **WHEN** `API_WORKERS` не задан
- **THEN** API работает одним процессом

#### Scenario: Неверное значение
- **WHEN** `API_WORKERS=0`
- **THEN** API не стартует, ошибка называет `API_WORKERS`

### Requirement: Несколько процессов API требуют общего состояния
При `API_WORKERS` больше 1 API MUST отказываться стартовать, если не задан `REDIS_URL`, не задан `JWT_SECRET` или хотя бы один из `PARSE_JOB_BACKEND`, `LLM_JOB_BACKEND`, `HH_SYNC_JOB_BACKEND` — `inprocess`. Ошибка SHALL называть переменную. Без них лимиты входа, одноразовость SSE-тикетов, подпись токенов и выполнение задач жили бы в памяти одного процесса.

#### Scenario: Без Redis
- **WHEN** `API_WORKERS=4`, `REDIS_URL` не задан
- **THEN** API не стартует, ошибка называет `REDIS_URL`

#### Scenario: Задачи inprocess
- **WHEN** `API_WORKERS=4`, `LLM_JOB_BACKEND=inprocess`
- **THEN** API не стартует, ошибка называет `LLM_JOB_BACKEND`

#### Scenario: Токен другого процесса
- **WHEN** `API_WORKERS=4`, пользователь вошёл, и следующий запрос принял другой процесс
- **THEN** запрос с этим access-токеном проходит

### Requirement: Процессы API делят пул соединений с БД
Лимит соединений API (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, по умолчанию 20 + 20) MUST действовать на все процессы API вместе: каждый получает свою долю, не меньше одного постоянного соединения и одного дополнительного, если дополнительные не отключены (`DB_MAX_OVERFLOW=0`). Сверх лимита SHALL открываться не больше одного служебного соединения на процесс.

#### Scenario: Четыре процесса
- **WHEN** `API_WORKERS=4` с умолчаниями пула, и API под нагрузкой
- **THEN** у API к базе не больше 40 соединений из пулов и не больше 4 служебных

### Requirement: Метрики Prometheus со всех процессов API
`/metrics` на `API_METRICS_PORT` MUST отдавать метрики HTTP и доменные метрики, просуммированные по всем процессам API, и SHALL обслуживаться одним процессом. Если он завершился, `/metrics` MUST снова отвечать не позже чем через минуту.

#### Scenario: Запросы в разные процессы
- **WHEN** `API_WORKERS=4`, и API принял 100 запросов, распределённых по процессам
- **THEN** счётчик HTTP-запросов в `/metrics` вырос на 100

### Requirement: Ожидание соединения с БД и очереди API ограничено
`API_QUEUE_TIMEOUT_SECONDS` MUST задавать, сколько запрос ждёт места обработки в процессе API: число от 0, по умолчанию 2. `DB_POOL_TIMEOUT` MUST задавать, сколько секунд процесс ждёт свободное соединение из пула: число больше 0; по умолчанию 5 у API и 30 у воркеров. Неверное значение SHALL быть ошибкой старта, которая называет переменную.

#### Scenario: Умолчания
- **WHEN** ни одна из переменных не задана
- **THEN** API ждёт место обработки до 2 с и соединение до 5 с, воркеры ждут соединение до 30 с

#### Scenario: Неверное значение
- **WHEN** `DB_POOL_TIMEOUT=0`
- **THEN** процесс не стартует, ошибка называет `DB_POOL_TIMEOUT`
