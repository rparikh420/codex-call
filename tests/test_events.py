import base64
import hashlib
import hmac
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_calls import REQUEST, settings

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()


def modules():
    assert importlib.util.find_spec("codex_call.events"), "event bridge missing"
    from codex_call import events, webhooks
    from codex_call.store import Store

    return events, webhooks, Store


class Callback:
    def __init__(self, status=200, echo=True):
        self.status = status
        self.echo = echo
        self.requests = []

    async def post(self, url, body, headers, hosts, *, acknowledgement_only=False):
        self.requests.append((url, body, headers))
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return 200, json.dumps(
                {"challenge": payload["challenge"] if self.echo else "wrong"}
            ).encode()
        return self.status, b""


def setup(tmp_path, callback=None):
    events, _, Store = modules()
    config = settings(tmp_path, events_enabled=True, event_callback_hosts=["hooks.example.com"])
    store = Store(config.database_path)
    manager = events.EventManager(store, config, sender=callback or Callback())
    call, _ = store.create_call(REQUEST, "one")
    return manager, store, call


def subscription(call=None, secret=SECRET):
    return {
        "name": "call.task_requested",
        "arguments": {"call_id": call["id"]} if call else {},
        "delivery": {
            "mode": "webhook",
            "url": "https://hooks.example.com/events",
            "secret": secret,
        },
        "cursor": None,
        "ttlMs": 60000,
    }


def test_standard_webhook_signature_has_independent_expected_bytes():
    _, webhooks, _ = modules()
    body = b'{"hello":"world"}'
    headers = webhooks.sign("evt-123", body, [SECRET], timestamp=1700000000)
    expected = base64.b64encode(
        hmac.new(b"k" * 32, b"evt-123.1700000000." + body, hashlib.sha256).digest()
    ).decode()
    assert headers["webhook-signature"] == "v1," + expected
    assert headers["webhook-id"] == "evt-123"
    assert headers["webhook-timestamp"] == "1700000000"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/",
        "https://127.0.0.1/",
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://[::1]/",
        "https://user@hooks.example.com/",
        "https://hooks.example.com/#x",
        "https://evil.example.com/",
        "https://hooks.example.com.evil/",
    ],
)
def test_callback_rejects_non_allowlisted_or_unsafe_urls(url):
    _, webhooks, _ = modules()
    with pytest.raises(webhooks.DestinationRejected):
        webhooks.validate_url(url, ["hooks.example.com"])


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "192.0.2.1"]
)
async def test_dns_resolver_rejects_nonpublic_answers(monkeypatch, address):
    _, webhooks, _ = modules()

    async def resolve(*args, **kwargs):
        return [
            {
                "hostname": "hooks.example.com",
                "host": address,
                "port": 443,
                "family": 2,
                "proto": 0,
                "flags": 0,
            }
        ]

    monkeypatch.setattr(webhooks.aiohttp.resolver.DefaultResolver, "resolve", resolve)
    resolver = webhooks.PublicResolver()
    try:
        with pytest.raises(webhooks.DestinationRejected):
            await resolver.resolve("hooks.example.com", 443)
    finally:
        await resolver.close()


async def test_challenge_refresh_rotation_unsubscribe_and_signatures(tmp_path):
    callback = Callback()
    manager, store, call = setup(tmp_path, callback)
    params = subscription(call)
    first = await manager.subscribe(params)
    assert first["cursor"] is None and first["truncated"] is False
    assert datetime.fromisoformat(first["refreshBefore"]) > datetime.now(UTC)
    challenge = json.loads(callback.requests[0][1])
    headers = callback.requests[0][2]
    assert challenge["type"] == "verification" and challenge["challenge"]
    expected = hmac.new(
        b"k" * 32,
        (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode()
        + callback.requests[0][1],
        hashlib.sha256,
    ).digest()
    assert headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode()
    assert headers["X-MCP-Subscription-Id"] == first["id"]
    new = "whsec_" + base64.b64encode(b"n" * 32).decode()
    refreshed = await manager.subscribe(subscription(call, new))
    assert refreshed["id"] == first["id"]
    assert json.loads(callback.requests[1][1])["challenge"] != challenge["challenge"]
    request = {"request_id": str(uuid4()), "prompt": "Which day?", "timeout_s": 120}
    store.create_task(call["id"], request)
    store.create_task(call["id"], request)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    await manager.tick()
    body, headers = callback.requests[-1][1:]
    assert len(headers["webhook-signature"].split(" ")) == 2
    payload = json.loads(body)
    assert payload["data"]["request_id"] == request["request_id"]
    assert payload["eventId"] == headers["webhook-id"]
    assert payload["cursor"] is None
    for key in [b"n" * 32, b"k" * 32]:
        digest = hmac.new(
            key,
            (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body,
            hashlib.sha256,
        ).digest()
        assert "v1," + base64.b64encode(digest).decode() in headers["webhook-signature"].split(" ")
    assert await manager.unsubscribe(params) == {}
    assert await manager.unsubscribe(params) == {}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


async def test_bad_challenge_does_not_persist_or_rotate(tmp_path):
    events, _, _ = modules()
    callback = Callback(echo=False)
    manager, store, call = setup(tmp_path, callback)
    with pytest.raises(events.EventError) as caught:
        await manager.subscribe(subscription(call))
    assert caught.value.code == -32015
    assert caught.value.reason == "challenge_failed"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


def test_nested_challenge_failure_preserves_subscriptions_through_authenticated_api(tmp_path):
    from fastapi.testclient import TestClient
    from test_mcp import rpc

    from codex_call.api import create_app

    app = create_app(
        settings(tmp_path, events_enabled=True, event_callback_hosts=["hooks.example.com"])
    )
    store = app.state.store
    app.state.events.sender = Callback()

    class NestedCallback:
        async def post(self, *args, **kwargs):
            # Valid JSON below the sender's byte cap, beyond the decoder's depth limit.
            return 200, b"[" * 30000 + b"0" + b"]" * 30000

    with TestClient(app) as client:
        assert rpc(client, "events/subscribe", subscription()).status_code == 200
        with store.connect() as db:
            original = [dict(row) for row in db.execute("SELECT * FROM subscriptions")]
        app.state.events.sender = NestedCallback()
        call, _ = store.create_call(REQUEST, "nested-challenge")
        new_secret = "whsec_" + base64.b64encode(b"n" * 32).decode()
        for params in [subscription(secret=new_secret), subscription(call)]:
            response = rpc(client, "events/subscribe", params)
            assert response.status_code == 400
            error = response.json()["error"]
            assert error["code"] == -32015
            assert error["data"] == {"reason": "challenge_failed"}
            with store.connect() as db:
                assert [dict(row) for row in db.execute("SELECT * FROM subscriptions")] == original


@pytest.mark.parametrize(
    "status,expected_attempts", [(500, 5), (429, 5), (408, 5), (410, 1), (413, 1), (400, 1)]
)
async def test_outbox_retries_are_bounded_and_survive_restart(tmp_path, status, expected_attempts):
    events, _, Store = modules()
    callback = Callback(status=status)
    manager, store, call = setup(tmp_path, callback)
    await manager.subscribe(subscription(call))
    request = {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120}
    store.create_task(call["id"], request)
    for _ in range(7):
        manager = events.EventManager(Store(store.path), manager.settings, sender=callback)
        with store.connect() as db:
            db.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        await manager.tick()
    deliveries = [r for r in callback.requests if "eventId" in json.loads(r[1])]
    assert len(deliveries) == expected_attempts
    assert len({r[1] for r in deliveries}) == 1
    with store.connect() as db:
        row = db.execute("SELECT status,attempts FROM outbox").fetchone()
        assert tuple(row) == ("failed", expected_attempts)


@pytest.mark.parametrize("stop", ["unsubscribe", "expiry", "revoked", "call_end"])
async def test_inactive_subscriptions_or_calls_never_deliver(tmp_path, stop):
    events, _, _ = modules()
    callback = Callback()
    manager, store, call = setup(tmp_path, callback)
    params = subscription(call)
    await manager.subscribe(params)
    store.create_task(call["id"], {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120})
    if stop == "unsubscribe":
        await manager.unsubscribe(params)
    elif stop == "expiry":
        with store.connect() as db:
            db.execute(
                "UPDATE subscriptions SET expires_at=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
    elif stop == "revoked":
        manager = events.EventManager(
            store,
            manager.settings.model_copy(
                update={"api_token": __import__("pydantic").SecretStr("z" * 40)}
            ),
            sender=callback,
        )
    else:
        store.transition(call["id"], "cancelled")
    await manager.tick()
    assert len(callback.requests) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0] == 0


async def test_filter_event_ids_and_transactional_insert(tmp_path):
    manager, store, call = setup(tmp_path)
    await manager.subscribe(subscription())
    other, _ = store.create_call(REQUEST, "two")
    request = {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120}
    store.create_task(call["id"], request)
    store.create_task(other["id"], request)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(DISTINCT event_id) FROM outbox").fetchone()[0] == 2
        db.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT, 'test'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(call["id"], request | {"request_id": str(uuid4())})
    assert len(store.list_tasks(call["id"])) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"cursor": "replay"},
        {"ttlMs": 0},
        {"delivery": {"mode": "webhook", "url": "https://hooks.example.com", "secret": "whsec_!!"}},
        {"arguments": {"call_id": str(uuid4())}},
    ],
)
async def test_invalid_subscription_is_not_activated(tmp_path, patch):
    events, _, _ = modules()
    manager, store, call = setup(tmp_path)
    with pytest.raises(events.EventError):
        await manager.subscribe(subscription(call) | patch)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


async def test_unsubscribe_uses_identity_without_a_secret(tmp_path):
    manager, store, call = setup(tmp_path)
    params = subscription(call)
    await manager.subscribe(params)
    del params["delivery"]["secret"]
    assert await manager.unsubscribe(params) == {}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


async def test_refresh_failure_keeps_old_secret_and_filtered_events(tmp_path):
    callback = Callback()
    manager, store, call = setup(tmp_path, callback)
    await manager.subscribe(subscription(call))
    callback.echo = False
    events, _, _ = modules()
    with pytest.raises(events.EventError):
        await manager.subscribe(subscription(call, "whsec_" + base64.b64encode(b"x" * 32).decode()))
    other, _ = store.create_call(REQUEST, "other")
    store.create_task(other["id"], {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120})
    with store.connect() as db:
        assert db.execute("SELECT secret FROM subscriptions").fetchone()[0] == SECRET
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


async def test_connection_pins_public_addresses_and_original_tls_hostname(monkeypatch):
    _, webhooks, _ = modules()
    import socket

    from aiohttp import ClientConnectorError

    resolved = []

    async def dns(self, host, port, family):
        resolved.append(host)
        return [
            {
                "hostname": host,
                "host": "93.184.215.14",
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
        ]

    connections = []

    async def connection(self, *args, **kwargs):
        connections.append(kwargs | {"addr_infos": list(kwargs["addr_infos"])})
        raise ClientConnectorError(kwargs["req"].connection_key, OSError("external boundary"))

    monkeypatch.setattr(webhooks.aiohttp.resolver.DefaultResolver, "resolve", dns)
    monkeypatch.setattr(webhooks.aiohttp.TCPConnector, "_wrap_create_connection", connection)
    with pytest.raises(ClientConnectorError):
        await webhooks.WebhookSender().post(
            "https://hooks.example.com/path", b"{}", {}, ["hooks.example.com"]
        )
    assert resolved == ["hooks.example.com"]
    assert connections[0]["server_hostname"] == "hooks.example.com"
    assert connections[0]["addr_infos"][0][4] == ("93.184.215.14", 443)
    assert connections[0]["ssl"].check_hostname is True


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError(), "timeout"),
        (ValueError("bad"), "challenge_failed"),
        (OSError("private network error"), "challenge_failed"),
    ],
)
async def test_challenge_errors_are_categorized(tmp_path, error, reason):
    events, _, _ = modules()

    class FailingCallback:
        async def post(self, *args, **kwargs):
            raise error

    manager, _, call = setup(tmp_path, FailingCallback())
    with pytest.raises(events.EventError) as caught:
        await manager.subscribe(subscription(call))
    assert caught.value.code == -32015 and caught.value.reason == reason


async def test_interrupted_delivery_counts_attempt_before_restart(tmp_path):
    import asyncio

    events, _, Store = modules()
    callback = Callback()
    manager, store, call = setup(tmp_path, callback)
    await manager.subscribe(subscription(call))
    store.create_task(call["id"], {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120})
    entered = asyncio.Event()

    class Interrupted:
        async def post(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    manager.sender = Interrupted()
    delivery = asyncio.create_task(manager.tick())
    await asyncio.wait_for(entered.wait(), 2)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    with store.connect() as db:
        row = db.execute("SELECT attempts,next_attempt_at FROM outbox").fetchone()
        assert row["attempts"] == 1
        assert datetime.fromisoformat(row["next_attempt_at"]) > datetime.now(UTC)
    restarted = events.EventManager(Store(store.path), manager.settings, sender=callback)
    await restarted.tick()
    assert len(callback.requests) == 1


async def test_real_tls_transport_no_redirects_chunking_and_response_limit(tmp_path, monkeypatch):
    import ipaddress
    import socket
    import ssl

    from aiohttp import web
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    _, webhooks, _ = modules()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hooks.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("hooks.example.com")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    client_tls = ssl.create_default_context(cafile=str(cert_file))
    monkeypatch.setattr(webhooks.aiohttp.connector, "_SSL_CONTEXT_VERIFIED", client_tls)
    seen = []

    async def callback(request):
        seen.append((request.path, await request.read()))
        if request.path == "/redirect":
            return web.Response(status=302, headers={"Location": "/chunked"})
        if request.path == "/large":
            return web.Response(body=b"x" * 65537)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"challenge":')
        import asyncio

        await asyncio.sleep(0.02)
        await response.write(b'"hello"}')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/{path}", callback)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock, ssl_context=tls).start()

    # This transport-only test permits its loopback DNS answer. Original resolver policy
    # is exercised separately against every private address class and mixed DNS answers.
    async def local_dns(self, host, port=0, family=0):
        assert host == "hooks.example.com"
        return [
            {
                "hostname": host,
                "host": str(ipaddress.ip_address("127.0.0.1")),
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
        ]

    monkeypatch.setattr(webhooks.PublicResolver, "resolve", local_dns)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    sender = webhooks.WebhookSender()
    url = f"https://hooks.example.com:{port}"
    try:
        status, body = await sender.post(
            url + "/chunked", b"exact bytes", {}, ["hooks.example.com"]
        )
        assert status == 200 and body == b'{"challenge":"hello"}'
        assert seen == [("/chunked", b"exact bytes")]
        status, _ = await sender.post(url + "/redirect", b"{}", {}, ["hooks.example.com"])
        assert status == 302 and len(seen) == 2
        with pytest.raises(ValueError, match="response_too_large"):
            await sender.post(url + "/large", b"{}", {}, ["hooks.example.com"])
    finally:
        await runner.cleanup()


async def test_dns_rebinding_and_mixed_answers_never_reach_socket(monkeypatch):
    import socket

    from aiohttp import ClientConnectorError

    _, webhooks, _ = modules()
    lookups = 0
    connections = []

    async def dns(self, host, port, family):
        nonlocal lookups
        lookups += 1
        addresses = ["93.184.215.14"] if lookups == 1 else ["93.184.215.14", "127.0.0.1"]
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for address in addresses
        ]

    async def connect(self, *args, **kwargs):
        connections.append(True)
        raise ClientConnectorError(kwargs["req"].connection_key, OSError("boundary"))

    monkeypatch.setattr(webhooks.aiohttp.resolver.DefaultResolver, "resolve", dns)
    monkeypatch.setattr(webhooks.aiohttp.TCPConnector, "_wrap_create_connection", connect)
    sender = webhooks.WebhookSender()
    with pytest.raises(ClientConnectorError):
        await sender.post("https://hooks.example.com", b"{}", {}, ["hooks.example.com"])
    with pytest.raises(webhooks.DestinationRejected):
        await sender.post("https://hooks.example.com", b"{}", {}, ["hooks.example.com"])
    assert lookups == 2 and len(connections) == 1


@pytest.mark.parametrize(
    "status,body_failure,expected_state",
    [
        (410, "timeout", "failed"),
        (413, "timeout", "failed"),
        (410, "broken", "failed"),
        (413, "broken", "failed"),
        (408, "oversized", "pending"),
        (429, "oversized", "pending"),
        (500, "oversized", "pending"),
        (200, "oversized", "delivered"),
        (204, "timeout", "delivered"),
        (202, "broken", "delivered"),
    ],
)
async def test_delivery_status_survives_irrelevant_response_body(
    tmp_path, monkeypatch, status, body_failure, expected_state
):
    import asyncio

    events, webhooks, Store = modules()

    class Response:
        def __init__(self, status, body=None, failure=None):
            self.status = status
            self.body = body
            self.failure = failure
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.close()

        def close(self):
            pass

        async def readexactly(self, limit):
            if self.failure == "timeout":
                raise TimeoutError("response body stalled")
            if self.failure == "broken":
                raise webhooks.aiohttp.ClientPayloadError("response body broken")
            if self.failure == "oversized":
                return b"x" * limit
            raise asyncio.IncompleteReadError(self.body, limit)

    def post(self, url, *, data, headers, allow_redirects):
        payload = json.loads(data)
        if payload.get("type") == "verification":
            return Response(200, json.dumps({"challenge": payload["challenge"]}).encode())
        return Response(status, failure=body_failure)

    # Substitute only the outbound response, preserving subscription verification,
    # durable task/outbox writes, sender policy and EventManager retry classification.
    monkeypatch.setattr(webhooks.aiohttp.ClientSession, "post", post)
    manager, store, call = setup(tmp_path, webhooks.WebhookSender())
    await manager.subscribe(subscription(call))
    store.create_task(call["id"], {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 120})
    await manager.tick()
    with store.connect() as db:
        row = db.execute("SELECT status,attempts FROM outbox").fetchone()
        assert tuple(row) == (expected_state, 1)
        db.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    restarted = events.EventManager(
        Store(store.path), manager.settings, sender=webhooks.WebhookSender()
    )
    await restarted.tick()
    with store.connect() as db:
        row = db.execute("SELECT status,attempts FROM outbox").fetchone()
        assert tuple(row) == (expected_state, 2 if expected_state == "pending" else 1)
