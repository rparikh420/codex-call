import base64
import importlib.util
import os
import socket
import sys
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_calls import HEADERS, REQUEST, WORKER_HEADERS, Provider, app_for, settings

VERSION = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def rpc(client, method, params=None, extra_headers=None):
    params = dict(params or {}) | {"_meta": META}
    headers = HEADERS | {"MCP-Protocol-Version": VERSION, "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = params["name"]
    headers.update(extra_headers or {})
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def tool(client, name, params):
    return rpc(
        client, "tools/call", {"name": "codex_call_" + name, "arguments": {"params": params}}
    )


def test_modern_discovery_tools_and_parity(tmp_path):
    app = app_for(tmp_path, Provider())
    with TestClient(app) as client:
        response = rpc(client, "server/discover")
        assert response.status_code == 200
        assert response.json()["result"]["resultType"] == "complete"
        assert "events" not in response.json()["result"]["capabilities"]
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert len(tools) == 7
        by_name = {t["name"]: t for t in tools}
        assert by_name["codex_call_place_call"]["annotations"]["idempotentHint"] is True
        assert by_name["codex_call_hangup_call"]["annotations"]["destructiveHint"] is True
        assert by_name["codex_call_get_call"]["annotations"]["readOnlyHint"] is True
        response = tool(client, "place_call", REQUEST | {"idempotency_key": "mcp"})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["resultType"] == "complete" and not result["isError"]
        assert result["content"][0]["type"] == "text"
        call = result["structuredContent"]
        assert call["mode"] == "simulate"
        assert (
            tool(client, "place_call", REQUEST | {"idempotency_key": "mcp"}).json()["result"][
                "structuredContent"
            ]["id"]
            == call["id"]
        )
        assert (
            tool(client, "get_call", {"call_id": call["id"]}).json()["result"]["structuredContent"][
                "id"
            ]
            == call["id"]
        )
        summary = tool(client, "list_calls", {}).json()["result"]["structuredContent"]["items"][0]
        assert (
            "task" not in summary and "personality" not in summary and summary["mode"] == "simulate"
        )
        assert tool(client, "get_transcript", {"call_id": call["id"]}).json()["result"][
            "structuredContent"
        ] == {"items": []}
        request_id = str(uuid4())
        client.post(
            f"/internal/calls/{call['id']}/tasks",
            headers=WORKER_HEADERS,
            json={"request_id": request_id, "prompt": "Day?", "timeout_s": 60},
        )
        assert (
            len(
                tool(client, "list_tasks", {"call_id": call["id"]}).json()["result"][
                    "structuredContent"
                ]["items"]
            )
            == 1
        )
        result = tool(
            client,
            "submit_task_result",
            {"call_id": call["id"], "request_id": request_id, "result": "Tuesday"},
        ).json()["result"]
        assert result["structuredContent"]["status"] == "completed"
        assert (
            tool(client, "hangup_call", {"call_id": call["id"]}).json()["result"][
                "structuredContent"
            ]["status"]
            == "cancelled"
        )
        assert rpc(client, "initialize").status_code == 404
        assert rpc(client, "events/list").status_code == 404
        assert client.get("/mcp", headers=HEADERS).status_code == 405
        assert rpc(client, "ping", extra_headers=WORKER_HEADERS).status_code == 401


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Mcp-Method": "wrong"}, -32020),
        ({"MCP-Protocol-Version": "wrong"}, -32020),
        ({"Mcp-Name": "wrong"}, -32020),
        ({"Mcp-Name": "=?base64?!!!!?="}, -32020),
        ({"Origin": "https://unapproved.example"}, -32020),
    ],
)
def test_mcp_rejects_mismatched_metadata_headers(tmp_path, headers, code):
    with TestClient(app_for(tmp_path, Provider())) as client:
        response = (
            tool(client, "list_calls", {})
            if not headers
            else rpc(
                client,
                "tools/call",
                {"name": "codex_call_list_calls", "arguments": {"params": {}}},
                headers,
            )
        )
        assert response.status_code in (400, 403)
        assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "patch",
    [
        {"id": None},
        {"id": True},
        {"jsonrpc": "1.0"},
        {"params": []},
        {"method": 42},
        {"params": {"_meta": []}},
        {"params": {"_meta": {}}},
    ],
)
def test_mcp_invalid_request_envelopes(tmp_path, patch):
    with TestClient(app_for(tmp_path, Provider())) as client:
        response = client.post(
            "/mcp",
            headers=HEADERS | {"MCP-Protocol-Version": VERSION, "Mcp-Method": "ping"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": META}} | patch,
        )
        assert response.status_code == 400
        assert "error" in response.json()


def test_mcp_version_body_limit_encoding_and_safe_errors(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        headers = HEADERS | {"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "ping"}
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "ping",
                "params": {
                    "_meta": META | {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}
                },
            },
        )
        assert response.json()["error"]["code"] == -32022
        assert response.json()["error"]["data"] == {
            "supported": [VERSION],
            "requested": "2099-01-01",
        }
        for content in [b"{bad", b"[]", b" " * 262145]:
            response = client.post("/mcp", headers=HEADERS, content=content)
            assert response.status_code in (400, 413)
            assert response.json()["error"]["code"] in (-32700, -32600)
        name = "codex_call_list_calls"
        encoded = "=?base64?" + base64.b64encode(name.encode()).decode() + "?="
        assert (
            rpc(
                client,
                "tools/call",
                {"name": name, "arguments": {"params": {}}},
                {"Mcp-Name": encoded},
            ).status_code
            == 200
        )
        for name, params in [
            ("get_call", {"call_id": "../secret"}),
            ("list_calls", {"limit": 101}),
            ("place_call", REQUEST),
            ("unknown", {}),
        ]:
            assert tool(client, name, params).json()["error"]["code"] == -32602
        missing = tool(client, "get_call", {"call_id": str(uuid4())}).json()["result"]
        assert missing["isError"] is True and missing["structuredContent"]["error"] == "not_found"


async def test_real_stdio_sdk_discovery_and_calls(tmp_path):
    assert importlib.util.find_spec("codex_call.mcp_server"), "stdio MCP missing"
    import uvicorn
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = settings(tmp_path)
    app = app_for(tmp_path, Provider())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_CALL_")}
        env.update(
            CODEX_CALL_API_TOKEN=config.api_token.get_secret_value(),
            CODEX_CALL_WORKER_TOKEN=config.worker_token.get_secret_value(),
            CODEX_CALL_API_BASE_URL=f"http://127.0.0.1:{port}",
        )
        args = StdioServerParameters(
            command=sys.executable, args=["-m", "codex_call.mcp_server"], env=env
        )
        async with stdio_client(args) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 7
            placed = await session.call_tool(
                "codex_call_place_call", {"params": REQUEST | {"idempotency_key": "stdio"}}
            )
            assert not placed.isError and placed.structuredContent["mode"] == "simulate"
            again = await session.call_tool(
                "codex_call_place_call", {"params": REQUEST | {"idempotency_key": "stdio"}}
            )
            assert again.structuredContent["id"] == placed.structuredContent["id"]
            listed = await session.call_tool("codex_call_list_calls", {"params": {}})
            assert listed.structuredContent["items"][0]["id"] == placed.structuredContent["id"]
            assert "task" not in listed.structuredContent["items"][0]
            invalid = await session.call_tool(
                "codex_call_get_call", {"params": {"call_id": "../private"}}
            )
            assert invalid.isError
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


def test_http_events_roundtrip_and_allowed_origin(tmp_path):
    from test_events import Callback, subscription

    app = app_for(
        tmp_path,
        Provider(),
        events_enabled=True,
        event_callback_hosts=["hooks.example.com"],
        mcp_allowed_origins=["https://allowed.example"],
    )
    app.state.events.sender = Callback()
    with TestClient(app) as client:
        assert "events" in rpc(client, "server/discover").json()["result"]["capabilities"]
        assert (
            rpc(client, "events/list").json()["result"]["events"][0]["name"]
            == "call.task_requested"
        )
        params = subscription()
        subscribed = rpc(client, "events/subscribe", params)
        assert subscribed.status_code == 200
        assert subscribed.json()["result"]["id"]
        assert rpc(client, "events/unsubscribe", params).json()["result"] == {
            "resultType": "complete"
        }
        assert (
            rpc(client, "ping", extra_headers={"Origin": "https://allowed.example"}).status_code
            == 200
        )
        params["cursor"] = "old"
        assert rpc(client, "events/subscribe", params).json()["error"]["code"] == -32602
        params = subscription()
        params["delivery"]["url"] = "https://127.0.0.1/"
        rejected = rpc(client, "events/subscribe", params).json()["error"]
        assert rejected["code"] == -32015 and rejected["data"]["reason"] == "destination_rejected"


def test_mcp_requires_each_transport_header_and_owner_auth(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": META, "name": "codex_call_list_calls", "arguments": {"params": {}}},
        }
        headers = HEADERS | {
            "MCP-Protocol-Version": VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "codex_call_list_calls",
        }
        for missing in ["MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"]:
            response = client.post(
                "/mcp", json=body, headers={k: v for k, v in headers.items() if k != missing}
            )
            assert response.status_code == 400 and response.json()["error"]["code"] == -32020
        assert client.post("/mcp", json=body).status_code == 401
