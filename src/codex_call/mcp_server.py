"""Shared constrained tools, official SDK stdio, and the negotiated HTTP MCP surface.

HTTP implements the 2026-07-28 request protocol only. Stdio uses the installed SDK's
negotiation. Public OAuth requires an external authorized gateway.
"""

import base64
import json
import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from codex_call.client import APIClient, APIError
from codex_call.config import Settings
from codex_call.events import EventError
from codex_call.models import CallRequest, StrictModel, TaskResult
from codex_call.service import CleanupFailed
from codex_call.store import Conflict, NotFound

VERSION = "2026-07-28"
PREFIX = "codex_call_"


class PlaceCall(CallRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)


class CallId(StrictModel):
    call_id: UUID


class Pagination(StrictModel):
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)


class SubmitResult(CallId, TaskResult):
    request_id: UUID


# name: (input type, description, read only, destructive, open world)
TOOLS = {
    "place_call": (
        PlaceCall,
        "Queue a phone call. Reuse the required idempotency key on retries. "
        "The returned mode distinguishes simulation from live calling; queued is not answered.",
        False,
        False,
        True,
    ),
    "get_call": (
        CallId,
        "Get a call record including its persisted mode and status.",
        True,
        False,
        False,
    ),
    "list_calls": (
        Pagination,
        "List bounded call summaries, including mode, without briefings.",
        True,
        False,
        False,
    ),
    "get_transcript": (
        CallId,
        "Get the full transcript for one call, including its speaker roles.",
        True,
        False,
        False,
    ),
    "hangup_call": (
        CallId,
        "End a call and request cleanup. Repeating this action is safe.",
        False,
        True,
        True,
    ),
    "list_tasks": (
        CallId,
        "Poll tasks requested during a call, including expiration and results.",
        True,
        False,
        False,
    ),
    "submit_task_result": (
        SubmitResult,
        "Complete a correlated pending task. Identical retries are accepted; "
        "changed or late results conflict.",
        False,
        False,
        False,
    ),
}


class LocalBackend:
    """Use the same service and Store as the HTTP API, without an HTTP loopback hop."""

    def __init__(self, app):
        self.app = app

    async def execute(self, name, params):
        store = self.app.state.store
        service = self.app.state.service
        call_id = params.get("call_id")
        if name == "place_call":
            key = params.pop("idempotency_key")
            if params["max_duration_s"] > self.app.state.settings.max_duration_s:
                raise APIError(422)
            result, _ = store.create_call(params, key, mode=self.app.state.settings.mode)
            service.notify()
            return result
        if name == "get_call":
            return store.get_call(call_id)
        if name == "list_calls":
            return {"items": store.list_calls(**params)}
        if name == "get_transcript":
            return {"items": store.transcript(call_id)}
        if name == "hangup_call":
            return await service.hangup(call_id)
        if name == "list_tasks":
            return {"items": store.list_tasks(call_id)}
        return store.submit_result(call_id, params["request_id"], params["result"])


class RemoteBackend:
    def __init__(self, client):
        self.client = client

    async def execute(self, name, params):
        if name == "place_call":
            key = params.pop("idempotency_key")
            return await self.client.request("POST", "/v1/calls", params, idempotency_key=key)
        if name == "list_calls":
            return await self.client.request(
                "GET", f"/v1/calls?limit={params['limit']}&offset={params['offset']}"
            )
        path = "/v1/calls/" + params["call_id"]
        suffix = {
            "get_call": "",
            "get_transcript": "/transcript",
            "hangup_call": "/hangup",
            "list_tasks": "/tasks",
            "submit_task_result": f"/tasks/{params.get('request_id', '')}/result",
        }[name]
        body = {"result": params["result"]} if name == "submit_task_result" else None
        method = "POST" if name in {"hangup_call", "submit_task_result"} else "GET"
        return await self.client.request(method, path + suffix, body)


class ToolService:
    def __init__(self, backend):
        self.backend = backend

    async def execute(self, name, params):
        try:
            result = await self.backend.execute(name, params.model_dump(mode="json"))
        except NotFound:
            raise ToolError("not_found") from None
        except Conflict:
            raise ToolError("conflict") from None
        except CleanupFailed:
            raise ToolError("cleanup_failed") from None
        except APIError as exc:
            message = {
                404: "not_found",
                409: "conflict",
                401: "unauthorized",
                422: "invalid_arguments",
            }.get(exc.status_code, "service_unavailable")
            raise ToolError(message) from None
        except Exception:
            raise ToolError("service_unavailable") from None
        if name == "list_calls":
            keys = ("id", "to_number", "status", "mode", "created_at", "updated_at", "error")
            result = {"items": [{key: call[key] for key in keys} for call in result["items"]]}
        return result

    async def call(self, full_name, arguments):
        if (
            not isinstance(full_name, str)
            or not full_name.startswith(PREFIX)
            or full_name[len(PREFIX) :] not in TOOLS
            or not isinstance(arguments, dict)
            or set(arguments) != {"params"}
        ):
            raise EventError()
        name = full_name[len(PREFIX) :]
        try:
            params = TOOLS[name][0].model_validate(arguments["params"])
        except ValidationError:
            raise EventError() from None
        return await self.execute(name, params)


def build_mcp(service, *, lifespan=None):
    server = FastMCP("Codex Call", lifespan=lifespan, log_level="ERROR")

    def wrapper(action):
        async def invoke(params):
            return await service.execute(action, params)

        return invoke

    for name, (model, description, read_only, destructive, open_world) in TOOLS.items():
        invoke = wrapper(name)
        invoke.__annotations__ = {"params": model, "return": dict[str, Any]}
        server.add_tool(
            invoke,
            name=PREFIX + name,
            description=description,
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=destructive,
                idempotentHint=True,
                openWorldHint=open_world,
            ),
            structured_output=True,
        )
    return server


def error_response(request_id, code, message, status=400, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error}, status_code=status)


def decode_name(value):
    if value is None:
        return None
    if value.startswith("=?base64?"):
        if not re.fullmatch(r"=\?base64\?[A-Za-z0-9+/]*={0,2}\?=", value):
            raise ValueError("invalid_header")
        value = base64.b64decode(value[9:-2], validate=True).decode("utf-8")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("invalid_header")
    return value


class HTTPMCP:
    def __init__(self, app):
        self.settings = app.state.settings
        self.events = app.state.events
        self.service = ToolService(LocalBackend(app))
        self.sdk = build_mcp(self.service)

    async def handle(self, request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                return error_response(None, -32600, "request_too_large", 413)
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return error_response(None, -32700, "parse_error")
        if not isinstance(envelope, dict):
            return error_response(None, -32600, "invalid_request")
        request_id = envelope.get("id")
        method, params = envelope.get("method"), envelope.get("params")
        if (
            envelope.get("jsonrpc") != "2.0"
            or type(request_id) not in (str, int)
            or not isinstance(method, str)
            or not isinstance(params, dict)
        ):
            return error_response(None, -32600, "invalid_request")
        meta = params.get("_meta")
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("io.modelcontextprotocol/protocolVersion"), str)
            or not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict)
        ):
            return error_response(request_id, -32600, "invalid_metadata")
        version = meta["io.modelcontextprotocol/protocolVersion"]
        if (
            request.headers.get("MCP-Protocol-Version") != version
            or request.headers.get("Mcp-Method") != method
        ):
            return error_response(request_id, -32020, "header_mismatch")
        if version != VERSION:
            return error_response(
                request_id,
                -32022,
                "unsupported_version",
                data={"supported": [VERSION], "requested": version},
            )
        origin = request.headers.get("Origin")
        if origin and origin not in self.settings.mcp_allowed_origins:
            return error_response(request_id, -32020, "origin_rejected", 403)
        if method == "tools/call":
            try:
                if decode_name(request.headers.get("Mcp-Name")) != params.get(
                    "name"
                ) or not params.get("name"):
                    raise ValueError("header_mismatch")
            except (ValueError, UnicodeError):
                return error_response(request_id, -32020, "header_mismatch")
        params = {key: value for key, value in params.items() if key != "_meta"}
        try:
            result = await self.dispatch(method, params)
        except EventError as exc:
            return error_response(
                request_id,
                exc.code,
                "method_not_found" if exc.code == -32601 else "request_failed",
                404 if exc.code == -32601 else 400,
                {"reason": exc.reason} if exc.code == -32015 else None,
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": result | {"resultType": "complete"}}
        )

    async def dispatch(self, method, params):
        if method == "server/discover":
            capabilities = {"tools": {}}
            if self.settings.events_enabled:
                capabilities["events"] = {}
            return {
                "protocolVersion": VERSION,
                "serverInfo": {"name": "Codex Call", "version": "0.1.0"},
                "capabilities": capabilities,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    tool.model_dump(mode="json", exclude_none=True)
                    for tool in await self.sdk.list_tools()
                ]
            }
        if method == "tools/call":
            if set(params) != {"name", "arguments"}:
                raise EventError()
            try:
                result = await self.service.call(params["name"], params["arguments"])
                is_error = False
            except ToolError as exc:
                result, is_error = {"error": str(exc)}, True
            return {
                "structuredContent": result,
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": is_error,
            }
        if self.settings.events_enabled:
            if method == "events/list":
                return {
                    "events": [
                        {
                            "name": "call.task_requested",
                            "description": "A call requests a correlated task result.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"call_id": {"type": "string", "format": "uuid"}},
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            if method == "events/subscribe":
                return await self.events.subscribe(params)
            if method == "events/unsubscribe":
                return await self.events.unsubscribe(params)
        raise EventError(-32601, "method_not_found")


def main():
    import argparse

    argparse.ArgumentParser(description="Run Codex Call MCP over stdio.").parse_args()
    client = APIClient(Settings())

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            await client.aclose()

    build_mcp(ToolService(RemoteBackend(client)), lifespan=lifespan).run(transport="stdio")


if __name__ == "__main__":
    main()
