# Spec Delta

## MODIFIED Requirements

### Requirement: Бэкенд задач — inprocess или rq
`PARSE_JOB_BACKEND`, `LLM_JOB_BACKEND` (настройки ядра) и `HH_SYNC_JOB_BACKEND` (настройка HH-сервиса) MUST принимать `inprocess` или `rq` без учёта регистра и пробелов; пустое значение или отсутствие переменной — `inprocess`. Другое значение SHALL быть ошибкой старта процесса, которая называет переменную.

#### Scenario: Опечатка в бэкенде
- **WHEN** `LLM_JOB_BACKEND=redis`
- **THEN** процесс не стартует, ошибка называет `LLM_JOB_BACKEND`

#### Scenario: Регистр и пробелы
- **WHEN** `PARSE_JOB_BACKEND=" RQ "`
- **THEN** прогоны идут через очередь RQ

#### Scenario: Опечатка в бэкенде HH
- **WHEN** HH-сервис стартует с `HH_SYNC_JOB_BACKEND=redis`
- **THEN** HH-сервис не стартует, ошибка называет `HH_SYNC_JOB_BACKEND`
