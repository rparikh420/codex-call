# Codex Call Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an independently implemented calling service to `rparikh420/codex-call`.

**Architecture:** An authenticated FastAPI service stores calls, tasks, and event deliveries in SQLite. A separate LiveKit voice worker handles SIP media and a prompt-based Haiku conversation. MCP tools connect clients to the API; a separate MCP 2.0 adapter handles event discovery and delivery.

**Tech Stack:** Python 3.11–3.13, FastAPI, SQLite, LiveKit Agents, Anthropic, Deepgram, ElevenLabs, Python MCP SDK, pytest, uv, Ruff.

## Global Constraints

Codex Call is an independently implemented, MIT-licensed service for making AI phone calls from Codex and other MCP clients.

The first release targets a single operator.

A local stdio MCP connection supports Codex without a public OAuth deployment.

Publication alone is not proof of a completed live call or a ChatGPT end-to-end integration.

Inbound answering, attended human transfers, recording storage, subscriptions/billing, and a customer dashboard are later extensions.

## Task 1: Durable authenticated call API

**Files:** `src/codex_call/config.py`, `models.py`, `store.py`, `service.py`, `api.py`, `tests/test_calls.py`.

**Interfaces:** `Store(path)`, `Store.create_call(request, key) -> (dict, bool)`, `Store.get_call(id) -> dict`, `Store.transition(id, status, error=None) -> dict`; `create_app(settings, provider=None) -> FastAPI`. Provider exposes async `dispatch(call)` and `hangup(call)`. Calls use UUID identifiers and a fixed room name derived from that UUID.

- [ ] Write tests that authenticate real HTTP requests against the app, reject invalid requests, retain transcripts across a reopened database, and ensure identical idempotency keys produce one dispatch while conflicting payloads return 409.

```python
response = client.post("/v1/calls", headers=headers, json=request)
assert response.status_code == 201
assert client.post("/v1/calls", headers=headers, json=request).json()["id"] == response.json()["id"]
```

- [ ] Run `uv run pytest tests/test_calls.py -q`, verify missing behavior fails, implement transactional call lifecycle, then rerun.
- [ ] Test cancellation cleanup, terminal-state protection, and startup recovery without duplicate dialing.
- [ ] Commit passing API behavior.

## Task 2: Prompt-based LiveKit phone worker

**Files:** `src/codex_call/provider.py`, `worker.py`, `voice.py`, `client.py`, `tests/test_voice.py`.

**Interfaces:** `LiveKitProvider(settings).dispatch(call)` creates a named agent dispatch with `call_id` metadata; `hangup(call)` deletes the room. The worker loads the full briefing through the internal API, reports call state, appends transcript items, and awaits external task results through authenticated HTTP. `build_prompt(task, personality) -> str` separates briefing data from agent policy.

- [ ] Test actual SDK request serialization for room, trunk, metadata, SIP destination, and answer waiting. Use a test transport at the remote boundary, never real dialing.

```python
assert request.room_name == call["room_name"]
assert request.sip_call_to == "+15555550100"
assert request.wait_until_answered is True
```

- [ ] Verify RED; implement LiveKit dispatch, SIP setup, provider-configured AgentSession, DTMF, hangup, transcript callbacks, disconnection cleanup, and deadline enforcement.
- [ ] Run installed SDK smoke checks and targeted tests; commit.

## Task 3: MCP tools and event/result bridge

**Files:** `src/codex_call/mcp_server.py`, `events.py`, `webhooks.py`, `tests/test_tasks.py`, `tests/test_mcp.py`, `tests/test_events.py`.

**Interfaces:** `Store.create_task(call_id, request) -> dict`, `Store.get_task(call_id, request_id) -> dict`, `Store.submit_result(call_id, request_id, result) -> dict`; task statuses are pending, completed, expired, cancelled. `EventManager.subscribe(params)` verifies callbacks, persists deterministic subscriptions, and returns expiration. `EventManager.tick()` processes the durable delivery outbox. `/mcp` implements negotiated MCP 2.0 discovery, tools, and Events; stdio uses the official Python SDK.

- [ ] Test task correlation, cancellation, expiry, duplicate same results, and conflicting results through authenticated API requests.

```python
assert submit_result(call_id, request_id, "Tuesday").status_code == 200
assert submit_result(call_id, request_id, "Wednesday").status_code == 409
```

- [ ] Verify RED; implement transactional task updates and MCP tools with input schemas and accurate annotations.
- [ ] Test event signatures against independently calculated HMAC, callback challenge validation, rejection of local destinations, subscription refresh/unsubscribe, bounded retries, and delivery persistence across restart.
- [ ] Verify RED; implement pinned-address HTTPS delivery with original TLS hostname, no redirects, Standard Webhooks signing, and a durable outbox.
- [ ] Exercise stdio discovery and tool calls with the real SDK client; commit.

## Task 4: Operable package and reviewed publication

**Files:** `README.md`, `.env.example`, `Dockerfile`, `compose.yaml`, `docs/setup.md`, `docs/events.md`, `skills/codex-call/SKILL.md`, `LICENSE`, `.github/workflows/ci.yml`.

- [ ] Document fresh install, separate API/worker processes, Telnyx SIP setup, Codex registration, and simulated versus live verification.
- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv build`, and command smoke checks.
- [ ] Obtain independent read-only review against the approved design; fix important defects and repeat affected checks.
- [ ] Audit every tracked file and cumulative commit for publication safety. Export only reviewed tracked files into a clean publication checkout using a GitHub noreply author.
- [ ] Verify active `gh` identity and destination owner before repository creation and before push. Create public `rparikh420/codex-call`, push the reviewed candidate, and compare the remote commit.
