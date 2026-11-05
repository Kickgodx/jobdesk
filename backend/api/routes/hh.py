"""Admin-only HH.ru: прокси к HH-сервису (``jobdesk_hh``).

Ядро проверяет вход, права администратора, ``Idempotency-Key`` и SSE-тикет, затем передаёт
запрос сервису с id пользователя; ответ сервиса (тело, статус, ошибка) отдаётся как есть.
Sync — фоновая durable-джоба сервиса: ``POST /hh/sync`` отвечает 202 с ``job_id``, статус и
итог — ``GET /hh/sync-jobs/{id}`` или SSE ``.../events``. Сервис недоступен — 503
``UPSTREAM_UNAVAILABLE`` (``retryable``), остальной API работает.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.api.deps import (
    CurrentUser,
    SseCredentials,
    authenticate_sse,
    require_admin,
    sse_credentials,
    verify_api_key,
)
from backend.db.session import session_scope
from backend.services import hh_service_client as hh_service
from backend.services import idempotency
from jobdesk.contracts.hh_service import (
    STATUS_PATH,
    SYNC_JOBS_PATH,
    SYNC_PATH,
    HhStatusResponse,
    HhSyncAccepted,
    HhSyncJobOut,
    HhSyncRequest,
    sync_job_path,
)
from jobdesk_platform.errors import CODE_FORBIDDEN, CODE_IDEMPOTENCY_IN_PROGRESS, AppError, http_error
from jobdesk_platform.service_client import UpstreamError, contract_detail
from jobdesk_platform.sse import SSE_HEADERS

_LOG = logging.getLogger(__name__)

router = APIRouter(
    prefix="/hh",
    tags=["hh"],
    dependencies=[Depends(verify_api_key)],
)
# EventSource не шлёт заголовки: доступ — одноразовым ?ticket=.
sse_router = APIRouter(prefix="/hh", tags=["hh"])


@router.get("/status", response_model=HhStatusResponse)
def hh_status(admin: CurrentUser = Depends(require_admin)) -> HhStatusResponse:
    return hh_service.call("GET", STATUS_PATH, model=HhStatusResponse, user_id=admin.id, idempotent=True)


@router.post("/sync", response_model=HhSyncAccepted, status_code=202)
def hh_sync(
    body: HhSyncRequest = HhSyncRequest(),
    admin: CurrentUser = Depends(require_admin),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> HhSyncAccepted | JSONResponse:
    """Поставить HH sync в очередь: 202 ``{job_id}``, 409 — sync уже активен."""
    actor = idempotency.actor_for_user(admin.id)
    scope = idempotency.SCOPE_HH_SYNC
    req_hash = idempotency.request_fingerprint(body.model_dump(mode="json"))
    begun = idempotency.begin(key=idempotency_key, scope=scope, actor=actor, request_hash=req_hash)
    if begun.cached is not None:
        return JSONResponse(status_code=begun.cached.status_code, content=begun.cached.body)
    if begun.in_progress:
        raise AppError(
            CODE_IDEMPOTENCY_IN_PROGRESS,
            "Запрос с этим Idempotency-Key ещё выполняется",
            status_code=409,
            retryable=True,
        )

    try:
        # Не идемпотентный вызов: повтор после ушедшего запроса создал бы второй sync.
        resp = hh_service.call(
            "POST",
            SYNC_PATH,
            model=HhSyncAccepted,
            json=body.model_dump(mode="json"),
            user_id=admin.id,
            idempotent=False,
        )
    except Exception:
        idempotency.abandon(key=idempotency_key, scope=scope, actor=actor)
        raise

    # Резерв закрывается сразу: повтор с тем же ключом вернёт тот же job_id, а не второй sync.
    idempotency.complete(
        key=idempotency_key,
        scope=scope,
        actor=actor,
        status_code=202,
        body=resp.model_dump(mode="json"),
        request_hash=req_hash,
    )
    return resp


@router.get("/sync-jobs", response_model=list[HhSyncJobOut])
def list_hh_sync_jobs(
    limit: int = Query(default=10, ge=1, le=100),
    admin: CurrentUser = Depends(require_admin),
) -> list[HhSyncJobOut]:
    """Последние sync, новые первыми: UI показывает активный или последний после перезагрузки."""
    return hh_service.call(
        "GET", SYNC_JOBS_PATH, model=HhSyncJobOut, params={"limit": limit}, user_id=admin.id, idempotent=True
    )


@router.get("/sync-jobs/{job_id}", response_model=HhSyncJobOut)
def get_hh_sync_job(job_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)) -> HhSyncJobOut:
    return hh_service.call("GET", sync_job_path(job_id), model=HhSyncJobOut, user_id=admin.id, idempotent=True)


@router.post("/sync-jobs/{job_id}/cancel", response_model=HhSyncJobOut)
def cancel_hh_sync_job(job_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)) -> HhSyncJobOut:
    """Отменить sync; ответ — его текущее состояние (и для уже завершённого). Отмена идемпотентна."""
    return hh_service.call(
        "POST", f"{sync_job_path(job_id)}/cancel", model=HhSyncJobOut, user_id=admin.id, idempotent=True
    )


# --- SSE ----------------------------------------------------------------------------------


def _authenticate_admin(creds: SseCredentials) -> int:
    with session_scope() as session:
        user = authenticate_sse(session, creds)
        if not user.is_admin:
            raise http_error(403, CODE_FORBIDDEN, "Admin only")
        return user.id


async def _open_events(job_id: uuid.UUID, user_id: int) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Открыть поток событий сервиса; ошибка до первого байта — обычный ответ с ошибкой."""
    client = hh_service.async_client()
    request = client.build_request("GET", f"{sync_job_path(job_id)}/events", headers=hh_service.stream_headers(user_id))
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise hh_service.unavailable(f"поток событий: {e}") from e
    if response.status_code == 200:
        return client, response
    try:
        await response.aread()
    finally:
        await response.aclose()
        await client.aclose()
    detail = contract_detail(response)
    if detail is None:
        raise hh_service.unavailable(f"поток событий: HTTP {response.status_code} не по контракту")
    raise hh_service.from_upstream_error(UpstreamError(response.status_code, detail))


async def _relay(client: httpx.AsyncClient, response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.HTTPError as e:
        # Обрыв посреди потока: клиент переподключится (EventSource) и получит актуальный статус.
        _LOG.warning("SSE HH sync: поток сервиса оборвался: %s", e)
    finally:
        await response.aclose()
        await client.aclose()


@sse_router.get("/sync-jobs/{job_id}/events")
async def hh_sync_job_events(job_id: uuid.UUID, creds: SseCredentials = Depends(sse_credentials)) -> StreamingResponse:
    """SSE: события ``status`` (джоба целиком: статус, прогресс, итог) до ``done``. Только admin.

    Auth: ``?ticket=`` или Bearer — проверяются до обращения к сервису.
    """
    user_id = await run_in_threadpool(_authenticate_admin, creds)
    client, response = await _open_events(job_id, user_id)
    return StreamingResponse(_relay(client, response), media_type="text/event-stream", headers=SSE_HEADERS)
