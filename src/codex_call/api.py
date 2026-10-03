"""Authenticated public and worker HTTP endpoints."""

import argparse
import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from codex_call.config import Settings
from codex_call.events import EventManager
from codex_call.mcp_server import HTTPMCP
from codex_call.models import CallRequest, CallUpdate, TaskRequest, TaskResult, TranscriptItem
from codex_call.service import CallService, CleanupFailed, SimulationProvider
from codex_call.store import Conflict, NotFound, Store


def create_app(settings: Settings, provider=None) -> FastAPI:
    if provider is None:
        if settings.mode == "simulate":
            provider = SimulationProvider()
        else:
            from codex_call.provider import LiveKitProvider

            provider = LiveKitProvider(settings)
    store = Store(settings.database_path)
    service = CallService(store, provider)
    events = EventManager(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        delivery = asyncio.create_task(events.run()) if settings.events_enabled else None
        try:
            yield
        finally:
            if delivery:
                delivery.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery
            await service.stop()

    app = FastAPI(title="Codex Call", lifespan=lifespan)
    app.state.store = store
    app.state.service = service
    app.state.settings = settings
    app.state.events = events
    mcp = HTTPMCP(app)
    bearer = HTTPBearer(auto_error=False)

    def auth(expected):
        async def verify(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ):
            if credentials is None or not secrets.compare_digest(
                credentials.credentials.encode("utf-8"), expected.get_secret_value().encode("utf-8")
            ):
                raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

        return verify

    owner = [Depends(auth(settings.api_token))]
    worker = [Depends(auth(settings.worker_token))]

    @app.exception_handler(NotFound)
    async def missing(request: Request, exc: NotFound):
        return JSONResponse({"detail": "call_not_found"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(CleanupFailed)
    async def cleanup_failed(request: Request, exc: CleanupFailed):
        return JSONResponse({"detail": "cleanup_failed"}, status_code=502)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.mode}

    @app.post("/v1/calls", dependencies=owner)
    async def create_call(
        request: CallRequest,
        response: Response,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if not idempotency_key.strip():
            raise HTTPException(422, "invalid_idempotency_key")
        if request.max_duration_s > settings.max_duration_s:
            raise HTTPException(422, "duration_exceeds_service_limit")
        call, created = store.create_call(request, idempotency_key, mode=settings.mode)
        response.status_code = 201 if created else 200
        service.notify()
        return call

    @app.get("/v1/calls", dependencies=owner)
    async def list_calls(
        limit: Annotated[int, Query(ge=1, le=100)] = 100, offset: Annotated[int, Query(ge=0)] = 0
    ):
        return {"items": store.list_calls(limit, offset)}

    @app.get("/v1/calls/{call_id}", dependencies=owner)
    async def get_call(call_id: str):
        return store.get_call(call_id)

    @app.get("/v1/calls/{call_id}/transcript", dependencies=owner)
    async def transcript(call_id: str):
        return {"items": store.transcript(call_id)}

    @app.post("/v1/calls/{call_id}/hangup", dependencies=owner)
    async def hangup(call_id: str):
        return await service.hangup(call_id)

    @app.get("/internal/calls/{call_id}", dependencies=worker)
    async def worker_get(call_id: str):
        return store.worker_call(call_id)

    @app.post("/internal/calls/{call_id}/claim", dependencies=worker)
    async def worker_claim(call_id: str):
        return store.claim_worker(call_id)

    @app.post("/internal/calls/{call_id}/cleanup", dependencies=worker)
    async def worker_cleanup(call_id: str):
        return store.mark_cleanup(call_id)

    @app.patch("/internal/calls/{call_id}", dependencies=worker)
    async def worker_update(call_id: str, update: CallUpdate):
        return store.transition(call_id, update.status, update.error)

    @app.post("/internal/calls/{call_id}/transcript", dependencies=worker)
    async def worker_transcript(call_id: str, item: TranscriptItem, response: Response):
        record, created = store.append_transcript(call_id, item)
        response.status_code = 201 if created else 200
        return record

    @app.post("/internal/calls/{call_id}/tasks", dependencies=worker)
    async def create_task(call_id: UUID, request: TaskRequest):
        return store.create_task(str(call_id), request)

    @app.get("/internal/calls/{call_id}/tasks/{request_id}", dependencies=worker)
    async def worker_task(call_id: UUID, request_id: UUID):
        return store.get_task(str(call_id), str(request_id))

    @app.get("/v1/calls/{call_id}/tasks", dependencies=owner)
    async def list_tasks(call_id: UUID):
        return {"items": store.list_tasks(str(call_id))}

    @app.get("/v1/calls/{call_id}/tasks/{request_id}", dependencies=owner)
    async def get_task(call_id: UUID, request_id: UUID):
        return store.get_task(str(call_id), str(request_id))

    @app.post("/v1/calls/{call_id}/tasks/{request_id}/result", dependencies=owner)
    async def task_result(call_id: UUID, request_id: UUID, request: TaskResult):
        return store.submit_result(str(call_id), str(request_id), request.result)

    @app.post("/mcp", dependencies=owner)
    async def mcp_request(request: Request):
        return await mcp.handle(request)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the single-process Codex Call API.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    args = parser.parse_args()
    uvicorn.run(create_app(Settings()), host=args.host, port=args.port)
