"""Фоновые HH sync (``hh.hh_sync_jobs``): claim с lease, fencing, reclaim — как parse_runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from jobdesk_hh.db.models import HhSyncJobRow
from jobdesk_hh.settings import get_hh_settings

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def new_lease_owner() -> str:
    """Уникальный fencing-token владельца lease (≤64)."""
    return f"hh-{uuid.uuid4().hex}"


def _lease_deadline(now: datetime) -> datetime:
    return now + timedelta(seconds=max(30, get_hh_settings().hh_sync_job_lease_seconds))


def create_hh_sync_job(session: Session, *, params: dict, requested_by: int | None) -> HhSyncJobRow:
    """Новая ``queued``-джоба. Второй активный sync — IntegrityError (uq_hh_sync_jobs_one_active)."""
    row = HhSyncJobRow(status="queued", params=params, requested_by=requested_by)
    session.add(row)
    session.flush()
    return row


def has_active_hh_sync_job(session: Session) -> bool:
    n = session.scalar(select(func.count()).select_from(HhSyncJobRow).where(HhSyncJobRow.status.in_(ACTIVE_STATUSES)))
    return int(n or 0) > 0


def get_hh_sync_job(session: Session, job_id: uuid.UUID) -> HhSyncJobRow | None:
    return session.get(HhSyncJobRow, job_id)


def list_hh_sync_jobs(session: Session, *, limit: int = 20) -> list[HhSyncJobRow]:
    stmt = select(HhSyncJobRow).order_by(HhSyncJobRow.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def set_hh_sync_job_rq_job_id(session: Session, job_id: uuid.UUID, rq_job_id: str | None) -> None:
    row = session.get(HhSyncJobRow, job_id)
    if row is not None:
        row.rq_job_id = rq_job_id
        session.flush()


def claim_hh_sync_job(session: Session, job_id: uuid.UUID, *, lease_owner: str) -> HhSyncJobRow | None:
    now = datetime.now(UTC)
    result = session.execute(
        update(HhSyncJobRow)
        .where(HhSyncJobRow.id == job_id, HhSyncJobRow.status == "queued")
        .values(
            status="running",
            started_at=now,
            lease_owner=lease_owner,
            lease_until=_lease_deadline(now),
            attempts=HhSyncJobRow.attempts + 1,
        )
    )
    if (result.rowcount or 0) != 1:
        return None
    session.flush()
    return session.get(HhSyncJobRow, job_id)


def _owned_running(job_id: uuid.UUID, lease_owner: str):
    return (
        HhSyncJobRow.id == job_id,
        HhSyncJobRow.status == "running",
        HhSyncJobRow.lease_owner == lease_owner,
    )


def touch_hh_sync_job_lease(session: Session, job_id: uuid.UUID, *, lease_owner: str) -> bool:
    """Продлить lease, только если он ещё наш (fencing)."""
    result = session.execute(
        update(HhSyncJobRow)
        .where(*_owned_running(job_id, lease_owner))
        .values(lease_until=_lease_deadline(datetime.now(UTC)))
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


def patch_hh_sync_job_progress(session: Session, job_id: uuid.UUID, progress: dict, *, lease_owner: str) -> bool:
    """Записать прогресс и продлить lease (fencing). False — lease уже чужой."""
    now = datetime.now(UTC)
    result = session.execute(
        update(HhSyncJobRow)
        .where(*_owned_running(job_id, lease_owner))
        .values(progress=progress, progress_updated_at=now, lease_until=_lease_deadline(now))
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


def finish_hh_sync_job(
    session: Session,
    job_id: uuid.UUID,
    status: str,
    *,
    lease_owner: str,
    result: dict | None,
    error_message: str | None = None,
) -> bool:
    """running → completed|failed|cancelled, только владельцем lease. False — lease перехвачен recover."""
    exec_result = session.execute(
        update(HhSyncJobRow)
        .where(*_owned_running(job_id, lease_owner))
        .values(
            status=status,
            finished_at=datetime.now(UTC),
            result=result,
            error_message=error_message,
            lease_owner=None,
            lease_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    return (exec_result.rowcount or 0) == 1


def request_hh_sync_job_cancel(session: Session, job_id: uuid.UUID) -> HhSyncJobRow | None:
    """Запросить отмену: ``queued`` → сразу ``cancelled``, ``running`` → флаг для воркера.

    Завершённый sync не меняется. Перевод из ``queued`` — условным UPDATE: если воркер
    успел взять sync, остаётся флаг, и останавливает его воркер. None — нет такого sync.
    """
    row = session.get(HhSyncJobRow, job_id)
    if row is None:
        return None
    if row.status in TERMINAL_STATUSES:
        return row
    cancelled = session.execute(
        update(HhSyncJobRow)
        .where(HhSyncJobRow.id == job_id, HhSyncJobRow.status == "queued")
        .values(
            status="cancelled",
            cancel_requested=True,
            finished_at=datetime.now(UTC),
            lease_owner=None,
            lease_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    if (cancelled.rowcount or 0) != 1:
        session.execute(
            update(HhSyncJobRow)
            .where(HhSyncJobRow.id == job_id, HhSyncJobRow.status == "running")
            .values(cancel_requested=True)
            .execution_options(synchronize_session=False)
        )
    session.flush()
    session.expire_all()
    return session.get(HhSyncJobRow, job_id)


def is_hh_sync_job_cancel_requested(session: Session, job_id: uuid.UUID) -> bool:
    return bool(session.scalar(select(HhSyncJobRow.cancel_requested).where(HhSyncJobRow.id == job_id)))


def fail_hh_sync_job(session: Session, job_id: uuid.UUID, error_message: str) -> None:
    """Без fencing: джоба не дошла до воркера (сбой постановки в очередь)."""
    row = session.get(HhSyncJobRow, job_id)
    if row is None or row.status in TERMINAL_STATUSES:
        return
    row.status = "failed"
    row.finished_at = datetime.now(UTC)
    row.error_message = error_message
    row.lease_owner = None
    row.lease_until = None
    session.flush()


def attempts_exhausted_message(attempts: int) -> str:
    return (
        f"Превышено число попыток ({attempts}): воркер не довёл HH sync до конца (падение процесса или потеря lease)."
    )


def list_hh_sync_jobs_with_expired_lease(session: Session, *, now: datetime) -> list[HhSyncJobRow]:
    stmt = select(HhSyncJobRow).where(
        HhSyncJobRow.status == "running",
        HhSyncJobRow.lease_until.is_not(None),
        HhSyncJobRow.lease_until < now,
    )
    return list(session.scalars(stmt))


def list_queued_hh_sync_jobs(session: Session, *, older_than: datetime | None) -> list[HhSyncJobRow]:
    stmt = select(HhSyncJobRow).where(HhSyncJobRow.status == "queued")
    if older_than is not None:
        stmt = stmt.where(HhSyncJobRow.created_at < older_than)
    return list(session.scalars(stmt))


def reclaim_expired_hh_sync_job(session: Session, job_id: uuid.UUID, *, now: datetime) -> str | None:
    """Атомарно running(expired lease) → queued|failed|cancelled. None — не удалось (lease продлён / уже не running).

    ``cancelled`` — отмена запрошена до того, как воркер пропал: повтор не нужен.
    """
    row = session.get(HhSyncJobRow, job_id)
    if row is None or row.status != "running" or row.lease_until is None:
        return None
    attempts = int(row.attempts or 0)
    base = {"rq_job_id": None, "lease_owner": None, "lease_until": None}
    if row.cancel_requested:
        outcome = "cancelled"
        values = {**base, "status": "cancelled", "finished_at": now}
    elif attempts >= max(1, get_hh_settings().hh_sync_job_max_attempts):
        outcome = "failed"
        values = {**base, "status": "failed", "finished_at": now, "error_message": attempts_exhausted_message(attempts)}
    else:
        outcome = "queued"
        values = {**base, "status": "queued", "started_at": None, "progress": None}
    # synchronize_session=False: identity map всё равно сбрасывается expire_all() ниже.
    result = session.execute(
        update(HhSyncJobRow)
        .where(
            HhSyncJobRow.id == job_id,
            HhSyncJobRow.status == "running",
            HhSyncJobRow.lease_until.is_not(None),
            HhSyncJobRow.lease_until < now,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    return outcome if (result.rowcount or 0) == 1 else None


def purge_old_hh_sync_jobs(session: Session, *, older_than: datetime, limit: int = 200) -> int:
    """Удалить до ``limit`` завершённых sync старше ``older_than``."""
    old = (
        select(HhSyncJobRow.id)
        .where(
            HhSyncJobRow.status.in_(TERMINAL_STATUSES),
            func.coalesce(HhSyncJobRow.finished_at, HhSyncJobRow.created_at) < older_than,
        )
        .limit(limit)
    )
    result = session.execute(delete(HhSyncJobRow).where(HhSyncJobRow.id.in_(old)))
    return int(result.rowcount or 0)
