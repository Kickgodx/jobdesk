# Spec Delta

## MODIFIED Requirements

### Requirement: Задача с истёкшим lease
API MUST публиковать метрику `db_jobs_lease_expired{kind}` — число задач в `running` с истёкшим или пустым lease — для `kind` = `parse`, `llm`, а HH-сервис — для `kind` = `hh_sync`, включая нули. Алерт `JobLeaseExpired` (`critical`) SHALL срабатывать, когда для любого `kind` значение больше 0 дольше 5 минут: recover (см. `platform/durable-jobs`) за это время такую задачу уже вернул бы в очередь.

#### Scenario: Воркер умер посреди прогона
- **WHEN** прогон в `running`, его lease истёк, и recover не поднимает его 5 минут
- **THEN** срабатывает `JobLeaseExpired` с `kind="parse"`

#### Scenario: Recover работает
- **WHEN** lease LLM-задачи истёк, и recover вернул её в `queued` через минуту
- **THEN** `JobLeaseExpired` не срабатывает

#### Scenario: Зависший HH sync
- **WHEN** HH sync в `running`, его lease истёк, и recover HH-сервиса не поднимает его 5 минут
- **THEN** срабатывает `JobLeaseExpired` с `kind="hh_sync"` по метрике HH-сервиса

### Requirement: Процесс недоступен
Алерт `TargetDown` (`critical`) SHALL срабатывать, когда скрейп backend, HH-сервиса (`hh-service`) или воркера (`llm-worker`, `parse-worker`, `hh-worker`) не проходит дольше 5 минут, в том числе у остановленного контейнера. Алерт `WorkerMissing` (`critical`) SHALL срабатывать, когда у типа воркера или у HH-сервиса нет ни одной цели скрейпа дольше 5 минут.

#### Scenario: Backend упал
- **WHEN** `/metrics` backend не отвечает 5 минут
- **THEN** срабатывает `TargetDown` для backend

#### Scenario: Воркер остановлен
- **WHEN** контейнер `hh-worker` остановлен
- **THEN** через 5 минут срабатывает `TargetDown` для `hh-worker`

#### Scenario: Воркер не запускался
- **WHEN** Prometheus стартовал, а контейнера `parse-worker` нет
- **THEN** через 5 минут срабатывает `WorkerMissing` для `parse-worker`

#### Scenario: HH-сервис не запускался
- **WHEN** Prometheus стартовал, а контейнера `hh-service` нет
- **THEN** через 5 минут срабатывает `WorkerMissing` для `jobdesk-hh-service`
