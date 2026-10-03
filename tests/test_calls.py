import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

OWNER = "owner-" + "a" * 32
WORKER = "worker-" + "b" * 32
HEADERS = {"Authorization": f"Bearer {OWNER}", "Idempotency-Key": "test-key"}
WORKER_HEADERS = {"Authorization": f"Bearer {WORKER}"}
REQUEST = {"to_number": "+14155550123", "task": "Ask when the shop opens"}


def foundation():
    # Missing implementation is an explicit failing assertion, not a collection error.
    assert importlib.util.find_spec("codex_call.api"), "authenticated API is missing"
    from codex_call.api import create_app
    from codex_call.config import Settings
    from codex_call.store import Store

    return create_app, Settings, Store


def settings(tmp_path, **kwargs):
    _, cls, _ = foundation()
    return cls(
        _env_file=None,
        api_token=OWNER,
        worker_token=WORKER,
        database_path=tmp_path / "calls.sqlite3",
        **kwargs,
    )


class Provider:
    """Stand-in for external telephony; durable state and scheduling remain real."""

    def __init__(self, fail_dispatch=False, fail_cleanup=0, blocking=False):
        self.dispatched = []
        self.cleaned = []
        self.fail_dispatch = fail_dispatch
        self.fail_cleanup = fail_cleanup
        self.blocking = blocking
        self.entered = threading.Event()

    async def dispatch(self, call):
        import asyncio

        self.dispatched.append(call)
        self.entered.set()
        if self.fail_dispatch:
            raise RuntimeError("secret-provider-credential-must-not-escape")
        if self.blocking:
            await asyncio.Event().wait()

    async def hangup(self, call):
        self.cleaned.append(call["id"])
        if self.fail_cleanup:
            self.fail_cleanup -= 1
            raise RuntimeError("secret-cleanup-error")


def app_for(tmp_path, provider=None, **kwargs):
    create_app, _, _ = foundation()
    return create_app(settings(tmp_path, **kwargs), provider=provider)


def wait_status(client, call_id, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"/v1/calls/{call_id}", headers=HEADERS).json()
        if result["status"] == expected:
            return result
        time.sleep(0.01)
    pytest.fail(f"call did not become {expected}: {result}")


def test_public_and_worker_tokens_cannot_cross_authentication_boundary(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        for headers in ({}, WORKER_HEADERS, {"Authorization": "Bearer wrong"}):
            assert client.post("/v1/calls", headers=headers, json=REQUEST).status_code == 401
        call = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()
        assert client.get(f"/internal/calls/{call['id']}", headers=HEADERS).status_code == 401
        assert (
            client.get(f"/internal/calls/{call['id']}", headers=WORKER_HEADERS).status_code == 200
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"to_number": "4155550123"},
        {"to_number": "+0123456789"},
        {"task": "   "},
        {"task": "a" * 12001},
        {"personality": "a" * 2001},
        {"max_duration_s": 29},
        {"max_duration_s": 1801},
        {"unexpected": True},
    ],
)
def test_invalid_call_payload_never_creates_a_job(tmp_path, patch):
    with TestClient(app_for(tmp_path, Provider())) as client:
        assert client.post("/v1/calls", headers=HEADERS, json=REQUEST | patch).status_code == 422
        assert client.get("/v1/calls", headers=HEADERS).json()["items"] == []


@pytest.mark.parametrize("key", [None, "", "   ", "a" * 201])
def test_missing_or_unbounded_idempotency_key_rejected(tmp_path, key):
    with TestClient(app_for(tmp_path, Provider())) as client:
        headers = {"Authorization": f"Bearer {OWNER}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        assert client.post("/v1/calls", headers=headers, json=REQUEST).status_code == 422


def test_idempotency_retries_dispatch_once_and_conflicting_payload_returns_409(tmp_path):
    provider = Provider()
    with TestClient(app_for(tmp_path, provider)) as client:
        first = client.post("/v1/calls", headers=HEADERS, json=REQUEST)
        assert first.status_code == 201
        call_id = first.json()["id"]
        retry = client.post("/v1/calls", headers=HEADERS, json=REQUEST)
        assert retry.status_code == 200
        assert retry.json()["id"] == call_id
        assert (
            client.post(
                "/v1/calls", headers=HEADERS, json=REQUEST | {"task": "Different"}
            ).status_code
            == 409
        )
        wait_status(client, call_id, "dispatching")
        assert len(provider.dispatched) == 1
        assert provider.dispatched[0]["room_name"] == f"codex-call-{call_id}"


def test_transcripts_survive_reopen_and_item_ids_detect_conflicts(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        route = f"/internal/calls/{call_id}/transcript"
        item = {"item_id": "item-1", "role": "assistant", "text": "Hello"}
        assert client.post(route, headers=WORKER_HEADERS, json=item).status_code == 201
        assert client.post(route, headers=WORKER_HEADERS, json=item).status_code == 200
        assert (
            client.post(route, headers=WORKER_HEADERS, json=item | {"text": "Changed"}).status_code
            == 409
        )
    with TestClient(app_for(tmp_path, Provider())) as client:
        items = client.get(f"/v1/calls/{call_id}/transcript", headers=HEADERS).json()["items"]
        assert len(items) == 1
        assert items[0]["text"] == "Hello"
        assert items[0]["role"] == "assistant"


def test_worker_cannot_overwrite_terminal_call_or_publish_raw_errors(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        wait_status(client, call_id, "dispatching")
        route = f"/internal/calls/{call_id}"
        assert (
            client.patch(route, headers=WORKER_HEADERS, json={"status": "dialing"}).status_code
            == 200
        )
        assert (
            client.patch(route, headers=WORKER_HEADERS, json={"status": "answered"}).status_code
            == 200
        )
        assert (
            client.patch(route, headers=WORKER_HEADERS, json={"status": "completed"}).status_code
            == 200
        )
        assert (
            client.patch(route, headers=WORKER_HEADERS, json={"status": "answered"}).status_code
            == 409
        )
        assert (
            client.patch(
                route,
                headers=WORKER_HEADERS,
                json={"status": "failed", "error": "api-key: private"},
            ).status_code
            == 422
        )
        assert client.get(f"/v1/calls/{call_id}", headers=HEADERS).json()["status"] == "completed"


def test_cancelled_call_retries_provider_cleanup_after_failure(tmp_path):
    provider = Provider(fail_cleanup=1)
    with TestClient(app_for(tmp_path, provider)) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        wait_status(client, call_id, "dispatching")
        route = f"/v1/calls/{call_id}/hangup"
        failed = client.post(route, headers=HEADERS)
        assert failed.status_code == 502
        assert "secret" not in failed.text
        assert client.get(f"/v1/calls/{call_id}", headers=HEADERS).json()["status"] == "cancelled"
        assert client.post(route, headers=HEADERS).status_code == 200
        assert provider.cleaned == [call_id, call_id]


def test_dispatch_failure_sanitized_and_room_cleanup_attempted(tmp_path):
    provider = Provider(fail_dispatch=True)
    with TestClient(app_for(tmp_path, provider)) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        call = wait_status(client, call_id, "failed")
        assert call["error"] == "dispatch_failed"
        assert provider.cleaned == [call_id]
        assert "secret" not in str(call)


def test_startup_resumes_queued_but_cleans_active_calls_without_redial(tmp_path):
    _, _, store_cls = foundation()
    from codex_call.models import CallRequest

    store = store_cls(settings(tmp_path).database_path)
    queued, _ = store.create_call(CallRequest(**REQUEST), "queued")
    active, _ = store.create_call(CallRequest(**REQUEST), "active")
    store.transition(active["id"], "dispatching")
    store.transition(active["id"], "dialing")
    provider = Provider()
    with TestClient(app_for(tmp_path, provider)) as client:
        interrupted = wait_status(client, active["id"], "failed")
        assert interrupted["error"] == "interrupted_restart"
        wait_status(client, queued["id"], "dispatching")
        assert [c["id"] for c in provider.dispatched] == [queued["id"]]
        assert provider.cleaned == [active["id"]]


def test_shutdown_cancels_blocked_dispatch_without_redialling_on_restart(tmp_path):
    provider = Provider(blocking=True)
    with TestClient(app_for(tmp_path, provider)) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        assert provider.entered.wait(2)
    restarted = Provider()
    with TestClient(app_for(tmp_path, restarted)) as client:
        assert wait_status(client, call_id, "failed")["error"] == "interrupted_restart"
        assert restarted.dispatched == []
        assert restarted.cleaned == [call_id]


def test_simulation_is_labelled_and_answered_until_hangup(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/health").json()["mode"] == "simulate"
        response = client.post(
            "/v1/calls", headers=HEADERS, json=REQUEST | {"task": "  Ask something  "}
        )
        call_id = response.json()["id"]
        assert wait_status(client, call_id, "answered")["task"] == "Ask something"
        assert (
            client.post(f"/v1/calls/{call_id}/hangup", headers=HEADERS).json()["status"]
            == "cancelled"
        )


def test_short_or_identical_bearer_tokens_are_rejected_before_startup(tmp_path):
    _, cls, _ = foundation()
    for owner, worker in [("short", WORKER), (OWNER, "short"), (OWNER, OWNER)]:
        with pytest.raises(ValidationError):
            cls(
                _env_file=None,
                api_token=owner,
                worker_token=worker,
                database_path=tmp_path / "calls.sqlite3",
            )


def test_atomic_concurrent_idempotency_and_claims(tmp_path):
    _, _, store_cls = foundation()
    from codex_call.models import CallRequest

    store = store_cls(settings(tmp_path).database_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(
            pool.map(lambda _: store.create_call(CallRequest(**REQUEST), "same"), range(12))
        )
    assert sum(new for _, new in created) == 1
    assert len({call["id"] for call, _ in created}) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda _: store.claim_queued(), range(12)))
    assert len([call for call in claimed if call is not None]) == 1


def test_list_pagination_and_missing_call_errors(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        for key in ["one", "two"]:
            assert (
                client.post(
                    "/v1/calls", headers=HEADERS | {"Idempotency-Key": key}, json=REQUEST
                ).status_code
                == 201
            )
        assert len(client.get("/v1/calls?limit=1&offset=1", headers=HEADERS).json()["items"]) == 1
        assert client.get("/v1/calls?limit=101", headers=HEADERS).status_code == 422
        assert client.get("/v1/calls/missing", headers=HEADERS).status_code == 404


def test_nonascii_bearer_returns_unauthorized_instead_of_crashing(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        response = client.get("/v1/calls", headers={b"Authorization": b"Bearer \xff"})
        assert response.status_code == 401


def test_hangup_still_cleans_when_worker_completes_between_read_and_cancel(tmp_path, monkeypatch):
    provider = Provider()
    app = app_for(tmp_path, provider)
    with TestClient(app) as client:
        call_id = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()["id"]
        wait_status(client, call_id, "dispatching")
        transition = app.state.store.transition

        def worker_wins(call_id, status, error=None):
            if status == "cancelled":
                transition(call_id, "completed")
            return transition(call_id, status, error)

        monkeypatch.setattr(app.state.store, "transition", worker_wins)
        result = client.post(f"/v1/calls/{call_id}/hangup", headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["status"] == "completed"
        assert provider.cleaned == [call_id]


@pytest.mark.parametrize("returned_status", ["dialing", "answered", "completed"])
async def test_non_simulation_provider_cannot_advance_worker_owned_states(
    tmp_path, returned_status
):
    from codex_call.models import CallRequest
    from codex_call.service import CallService

    _, _, store_cls = foundation()
    store = store_cls(settings(tmp_path).database_path)
    call, _ = store.create_call(CallRequest(**REQUEST), "returned-state")
    claimed = store.claim_queued()

    class ReturningProvider(Provider):
        async def dispatch(self, call):
            return {"status": returned_status}

    service = CallService(store, ReturningProvider())
    await service._dispatch(claimed)
    assert store.get_call(call["id"])["status"] == "dispatching"


def test_invalid_settings_error_does_not_print_input_bearer_tokens(tmp_path):
    _, cls, _ = foundation()
    with pytest.raises(ValidationError) as caught:
        cls(
            _env_file=None,
            api_token=OWNER,
            worker_token=OWNER,
            database_path=tmp_path / "calls.sqlite3",
        )
    assert OWNER not in str(caught.value)
    assert "owner-" not in str(caught.value)
