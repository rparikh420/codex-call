# Events and hosted MCP

The task bridge is optional. A voice worker requests external work; an independently authorized host performs it and submits a correlated result. Codex Call does not gain other apps' credentials or automatically invoke plugins. Without Events, an owner client can poll `codex_call_list_tasks`.

## Protocol boundary

Local stdio uses the official MCP FastMCP SDK's negotiation. Hosted `POST /mcp` implements only the modern HTTP protocol version `2026-07-28`, with `server/discover`, `ping`, `tools/list`, `tools/call` and opt-in `events/list`, `events/subscribe`, `events/unsubscribe`. It is not SDK v1 streamable HTTP; legacy `initialize` and `GET /mcp` are unsupported. Every successful HTTP result includes `resultType: "complete"`.

HTTP requests require the owner bearer token, `Content-Type: application/json`, `MCP-Protocol-Version: 2026-07-28`, and `Mcp-Method` matching the JSON-RPC method. `tools/call` additionally requires `Mcp-Name` matching its tool name. Each request has one non-null integer or string `id`, and `params._meta` contains both `io.modelcontextprotocol/protocolVersion` and `io.modelcontextprotocol/clientCapabilities`. Batches are unsupported. Requests are bounded to 256KiB.

For example, this is a `tools/call` body (supply the headers above, plus `Mcp-Name: codex_call_list_calls`):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {}
    },
    "name": "codex_call_list_calls",
    "arguments": {"params": {"limit": 20, "offset": 0}}
  }
}
```

`CODEX_CALL_MCP_ALLOWED_ORIGINS` defaults to `[]`: any supplied `Origin` is rejected unless explicitly listed. Requests without `Origin` remain bearer-protected. An authorized deployment gateway must provide public HTTPS and compatible authorization for a ChatGPT host. Hosts requiring OAuth need an external OAuth gateway; no OAuth server is built here. Host Events compatibility and external callback delivery remain unverified. The [MCP Events specification](https://developers.openai.com/plugins/build/mcp-events) describes the target host contract; a local protocol test is not a live ChatGPT acceptance test.

## Opt in and verify a subscription

Deploy and authorize the callback host yourself before enabling delivery. It must accept only valid signed webhooks, validate freshness, echo a verification challenge, deduplicate events, and have the actual tools and user authorization needed to perform tasks.

Set these application settings in your protected `.env`, replacing the illustrative hostname with your authorized public host:

```dotenv
CODEX_CALL_EVENTS_ENABLED=true
CODEX_CALL_EVENT_CALLBACK_HOSTS=["callbacks.example.com"]
```

The allowlist compares exact hostnames. Callbacks require HTTPS and public DNS answers. IP literals, private/mixed DNS answers, userinfo, fragments, redirects and environment proxies are rejected; each connection uses newly validated DNS and normal certificate verification. Challenge requests have a five-second bound and 64KiB response cap. Ordinary delivery decisions use the received HTTP status; response bodies do not prove task completion.

Generate a separate Standard Webhooks secret without printing it into logs: `whsec_` followed by base64 encoding of 24–64 random bytes. For example, in your own callback secret configuration code, use `"whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()`. Store the same value securely at the callback and in the subscription request; do not reuse API tokens.

Use `Mcp-Method: events/subscribe` with a body like this. Replace the secret placeholder before submitting (the placeholder is deliberately not a valid key):

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "events/subscribe",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {"events": {}}
    },
    "name": "call.task_requested",
    "arguments": {},
    "delivery": {
      "mode": "webhook",
      "url": "https://callbacks.example.com/call-events",
      "secret": "replace-with-generated-whsec-secret"
    },
    "cursor": null,
    "ttlMs": 86400000
  }
}
```

Use `arguments: {"call_id": "<known-call-uuid>"}` to filter one call. Default and maximum TTL are 24 hours (`86400000` milliseconds); refresh before expiry. The signed verification body is `{"type":"verification","challenge":"..."}`. Verify its signature and return a 2xx JSON response containing that exact `challenge` value. A failed challenge never activates a subscription or changes its current secret.

Signatures follow the [Standard Webhooks format](https://www.standardwebhooks.com/): headers `webhook-id`, `webhook-timestamp`, `webhook-signature` contain HMAC-SHA256 signatures over `id.timestamp.raw_body`. Verify exact raw bytes with your securely configured key and a freshness tolerance. The request also contains `X-MCP-Subscription-Id`. A refresh with a new secret must pass a new challenge; thereafter old and new keys both sign delivery for five minutes. Subscription identity binds the owner token, original URL, name and canonical arguments; changing the owner token revokes delivery under the old ownership.

Unsubscribe with `Mcp-Method: events/unsubscribe` and the same original `name`, `arguments`, `delivery.mode` and `delivery.url`; omit `delivery.secret`. It is idempotent. There is no replay cursor. A later subscription does not replay earlier requests; poll pending tasks to recover.

## Deliveries and task results

A task event has this shape (UUIDs and timestamps are illustrative):

```json
{
  "eventId": "00000000-0000-4000-8000-000000000001",
  "name": "call.task_requested",
  "timestamp": "2026-10-03T12:00:00+00:00",
  "data": {
    "call_id": "00000000-0000-4000-8000-000000000002",
    "request_id": "00000000-0000-4000-8000-000000000003",
    "prompt": "Find the requested information within the authorized task.",
    "expires_at": "2026-10-03T12:01:00+00:00"
  },
  "cursor": null
}
```

Delivery is at least once. Deduplicate `eventId`; retries keep the same event and body, with a fresh signature timestamp. Persist acknowledgment handling before returning 2xx. Network failures, 408, 429 and 5xx retry with bounded exponential backoff, at most five total attempts (including an interrupted attempt). Other non-2xx statuses, including 410 and 413, stop delivery. Subscription expiry/revocation/unsubscribe, task completion/expiry or call termination removes pending delivery. An already in-flight request cannot be recalled.

A 2xx webhook acknowledgment confirms receipt only. Check `codex_call_list_tasks` for current `pending` status and expiry, perform only separately authorized work, and call `codex_call_submit_task_result` with `arguments: {"params": {"call_id": "...", "request_id": "...", "result": "actual result"}}`. Results are strings up to 12000 characters. Identical results are idempotent; changed, expired or cancelled submissions conflict. Phone-worker task timeouts are 5–120 seconds and bounded by remaining call duration. Ending a call cancels pending tasks.

The owner HTTP alternatives are `GET /v1/calls/{call_id}/tasks`, `GET /v1/calls/{call_id}/tasks/{request_id}`, and `POST /v1/calls/{call_id}/tasks/{request_id}/result` with `{"result":"actual result"}`. These use the owner bearer token. Worker task creation is internal and requires the separate worker token.
