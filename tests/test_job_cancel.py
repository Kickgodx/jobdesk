"""Отмена LLM-задач и HH sync: флаг ``cancel_requested`` и статус ``cancelled``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from tests.conftest import new_user

from backend.db import llm_job_repository as lj
from backend.db.migrate import alembic_ini_path
from backend.db.models import ResumeRow, VacancyRow
from backend.db.session import session_scope


def test_migration_adds_cancel_requested(pg_empty_db):
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "030_llm_results_requested_model")

    engine = create_engine(pg_empty_db)
    llm_id, hh_id = uuid.uuid4(), uuid.uuid4()
    with sessionmaker(bind=engine)() as s:
        user_id = new_user(s)
        s.execute(
            text("INSERT INTO llm_jobs (id, user_id, kind, status, force) VALUES (:id, :u, 'import', 'done', false)"),
            {"id": llm_id, "u": user_id},
        )
        s.execute(
            text("INSERT INTO hh_sync_jobs (id, status, params) VALUES (:id, 'completed', '{}')"),
            {"id": hh_id},
        )
        s.commit()

    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT cancel_requested FROM llm_jobs")).scalar_one() is False
        # На head таблица HH sync — в схеме hh (миграция 054); downgrade возвращает её в public.
        assert conn.execute(text("SELECT cancel_requested FROM hh.hh_sync_jobs")).scalar_one() is False
        conn.execute(text("UPDATE llm_jobs SET status = 'cancelled', cancel_requested = true"))
        conn.execute(text("UPDATE hh.hh_sync_jobs SET status = 'cancelled', cancel_requested = true"))

    command.downgrade(cfg, "030_llm_results_requested_model")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM llm_jobs")).scalar_one() == "error"
        assert conn.execute(text("SELECT status FROM hh_sync_jobs")).scalar_one() == "failed"
        cols = conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns"
                " WHERE column_name = 'cancel_requested' AND table_name IN ('llm_jobs', 'hh_sync_jobs')"
            )
        ).all()
    assert cols == []
    engine.dispose()


# --- LLM-задачи: запрос отмены в репозитории ---


def _llm_pair(session) -> tuple[int, int, str]:
    user_id = new_user(session)
    resume = ResumeRow(user_id=user_id, name="CV", text="python")
    session.add(resume)
    session.add(
        VacancyRow(
            id="v1", source_post_key="ch-v1", source_channel="ch", source_url="u", raw_text="x", cleaned_text="x"
        )
    )
    session.flush()
    return user_id, resume.id, "v1"


def _llm_job(session, *, status: str = "queued", kind: str = "match"):
    user_id, resume_id, vacancy_id = _llm_pair(session)
    row = lj.create_llm_job(session, user_id=user_id, kind=kind, resume_id=resume_id, vacancy_id=vacancy_id)
    if status == "running":
        assert lj.claim_llm_job(session, row.id, lease_owner="w1") is not None
    elif status != "queued":
        lj.set_llm_job_status(session, row.id, status)
    session.flush()
    return row.id


def test_cancel_queued_llm_job_is_immediate(db_session):
    job_id = _llm_job(db_session)

    row = lj.request_llm_job_cancel(db_session, job_id)

    assert (row.status, row.cancel_requested) == ("cancelled", True)
    assert row.finished_at is not None
    # Отменённую задачу воркер уже не возьмёт.
    assert lj.claim_llm_job(db_session, job_id, lease_owner="w1") is None


def test_cancel_running_llm_job_sets_flag(db_session):
    job_id = _llm_job(db_session, status="running")

    row = lj.request_llm_job_cancel(db_session, job_id)

    assert (row.status, row.cancel_requested) == ("running", True)
    assert lj.is_llm_job_cancel_requested(db_session, job_id) is True


def test_cancel_finished_llm_job_is_noop(db_session):
    job_id = _llm_job(db_session, status="done")

    row = lj.request_llm_job_cancel(db_session, job_id)

    assert (row.status, row.cancel_requested) == ("done", False)
    assert lj.request_llm_job_cancel(db_session, uuid.uuid4()) is None


def test_cancelled_llm_job_frees_pair_and_is_purged(db_session):
    job_id = _llm_job(db_session)
    row = lj.get_llm_job(db_session, job_id)
    pair = {"user_id": row.user_id, "kind": row.kind, "resume_id": row.resume_id, "vacancy_id": row.vacancy_id}
    lj.request_llm_job_cancel(db_session, job_id)

    assert lj.find_active_llm_job(db_session, **pair) is None
    lj.create_llm_job(db_session, **pair)  # partial unique по активным — не мешает
    db_session.flush()

    later = datetime.now(UTC) + timedelta(days=1)
    assert lj.purge_finished_llm_jobs(db_session, older_than=later) == 1
    assert lj.get_llm_job(db_session, job_id) is None


# --- LLM-задачи: POST /llm-jobs/{id}/cancel и SSE ---


def _register(client, email: str) -> tuple[str, int]:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "secret1"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["user"]["id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _queued_job_for(user_id: int, *, status: str = "queued") -> uuid.UUID:
    with session_scope() as session:
        resume = ResumeRow(user_id=user_id, name="CV", text="python")
        session.add(resume)
        if session.get(VacancyRow, "v1") is None:
            session.add(
                VacancyRow(
                    id="v1",
                    source_post_key="ch-v1",
                    source_channel="ch",
                    source_url="u",
                    raw_text="x",
                    cleaned_text="x",
                )
            )
        session.flush()
        row = lj.create_llm_job(session, user_id=user_id, kind="match", resume_id=resume.id, vacancy_id="v1")
        if status != "queued":
            lj.set_llm_job_status(session, row.id, status)
        return row.id


def test_cancel_llm_job_api(api_client):
    token, user_id = _register(api_client, "a@example.com")
    other, _ = _register(api_client, "b@example.com")
    job_id = _queued_job_for(user_id)

    assert api_client.post(f"/api/v1/llm-jobs/{job_id}/cancel", headers=_auth(other)).status_code == 404
    assert api_client.post(f"/api/v1/llm-jobs/{uuid.uuid4()}/cancel", headers=_auth(token)).status_code == 404

    r = api_client.post(f"/api/v1/llm-jobs/{job_id}/cancel", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("cancelled", True)
    # Повтор идемпотентен.
    again = api_client.post(f"/api/v1/llm-jobs/{job_id}/cancel", headers=_auth(token))
    assert (again.status_code, again.json()["status"]) == (200, "cancelled")
    assert api_client.get(f"/api/v1/llm-jobs/{job_id}", headers=_auth(token)).json()["status"] == "cancelled"


def test_cancel_finished_llm_job_returns_current_state(api_client):
    token, user_id = _register(api_client, "a@example.com")
    job_id = _queued_job_for(user_id, status="error")

    r = api_client.post(f"/api/v1/llm-jobs/{job_id}/cancel", headers=_auth(token))

    assert r.status_code == 200
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("error", False)


def test_llm_job_sse_closes_on_cancelled(api_client):
    token, user_id = _register(api_client, "a@example.com")
    job_id = _queued_job_for(user_id)
    api_client.post(f"/api/v1/llm-jobs/{job_id}/cancel", headers=_auth(token))

    with api_client.stream("GET", f"/api/v1/llm-jobs/{job_id}/events", headers=_auth(token)) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
    assert "event: status" in body
    assert '"status":"cancelled"' in body.replace(" ", "")
