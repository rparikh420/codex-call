from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_calls import HEADERS, REQUEST, WORKER_HEADERS, Provider, app_for, wait_status


def create(client):
    call = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()
    request = {"request_id": str(uuid4()), "prompt": "  Which day?  ", "timeout_s": 5}
    route = f"/internal/calls/{call['id']}/tasks"
    response = client.post(route, headers=WORKER_HEADERS, json=request)
    assert response.status_code == 200
    return call, request, response.json()


def test_task_correlation_duplicates_and_roles(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call, request, task = create(client)
        internal = f"/internal/calls/{call['id']}/tasks"
        route = f"/v1/calls/{call['id']}/tasks/{request['request_id']}"
        assert task["prompt"] == "Which day?"
        assert task["status"] == "pending" and task["result"] is None
        assert client.post(internal, headers=WORKER_HEADERS, json=request).json() == task
        assert (
            client.post(
                internal, headers=WORKER_HEADERS, json=request | {"prompt": "Other"}
            ).status_code
            == 409
        )
        assert client.post(internal, headers=HEADERS, json=request).status_code == 401
        assert client.get(route, headers=WORKER_HEADERS).status_code == 401
        assert (
            client.get(internal + "/" + request["request_id"], headers=WORKER_HEADERS).json()
            == task
        )
        assert client.get(f"/v1/calls/{call['id']}/tasks", headers=HEADERS).json()["items"] == [
            task
        ]
        for result in ["Tuesday", "Tuesday"]:
            response = client.post(route + "/result", headers=HEADERS, json={"result": result})
            assert response.status_code == 200
            assert response.json()["result"] == "Tuesday"
        assert (
            client.post(
                route + "/result", headers=HEADERS, json={"result": "Wednesday"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                route.replace(call["id"], str(uuid4())) + "/result",
                headers=HEADERS,
                json={"result": "Tuesday"},
            ).status_code
            == 404
        )


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
def test_terminal_call_cancels_tasks_and_rejects_late_results(tmp_path, terminal):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call, request, _ = create(client)
        wait_status(client, call["id"], "dispatching")
        assert (
            client.patch(
                f"/internal/calls/{call['id']}", headers=WORKER_HEADERS, json={"status": terminal}
            ).status_code
            == 200
        )
        route = f"/v1/calls/{call['id']}/tasks/{request['request_id']}"
        assert client.get(route, headers=HEADERS).json()["status"] == "cancelled"
        assert (
            client.post(route + "/result", headers=HEADERS, json={"result": "late"}).status_code
            == 409
        )
        assert (
            client.post(
                f"/internal/calls/{call['id']}/tasks",
                headers=WORKER_HEADERS,
                json=request | {"request_id": str(uuid4())},
            ).status_code
            == 409
        )


def test_expiry_persists_even_when_result_submission_conflicts(tmp_path):
    app = app_for(tmp_path, Provider())
    with TestClient(app) as client:
        call, request, _ = create(client)
        with app.state.store.connect() as db:
            db.execute("UPDATE tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        route = f"/v1/calls/{call['id']}/tasks/{request['request_id']}"
        assert (
            client.post(route + "/result", headers=HEADERS, json={"result": "late"}).status_code
            == 409
        )
        with app.state.store.connect() as db:
            assert db.execute("SELECT status FROM tasks").fetchone()[0] == "expired"


@pytest.mark.parametrize(
    "patch",
    [
        {"request_id": "../bad"},
        {"prompt": " "},
        {"timeout_s": 4},
        {"timeout_s": 121},
        {"prompt": "x" * 4001},
    ],
)
def test_invalid_task_requests(tmp_path, patch):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call = client.post("/v1/calls", headers=HEADERS, json=REQUEST).json()
        body = {"request_id": str(uuid4()), "prompt": "Day?", "timeout_s": 5} | patch
        assert (
            client.post(
                f"/internal/calls/{call['id']}/tasks", headers=WORKER_HEADERS, json=body
            ).status_code
            == 422
        )


def test_result_text_is_preserved_exactly_for_idempotency(tmp_path):
    with TestClient(app_for(tmp_path, Provider())) as client:
        call, request, _ = create(client)
        route = f"/v1/calls/{call['id']}/tasks/{request['request_id']}/result"
        assert (
            client.post(route, headers=HEADERS, json={"result": " Tuesday\n"}).json()["result"]
            == " Tuesday\n"
        )
        assert client.post(route, headers=HEADERS, json={"result": "Tuesday"}).status_code == 409


def test_deadline_crossed_during_result_transaction_persists_expiry(tmp_path, monkeypatch):
    from codex_call import store as module

    app = app_for(tmp_path, Provider())
    with TestClient(app) as client:
        call, request, _ = create(client)
        values = iter(["2000-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"])
        monkeypatch.setattr(module, "now", lambda: next(values, "2099-01-01T00:00:00+00:00"))
        route = f"/v1/calls/{call['id']}/tasks/{request['request_id']}/result"
        assert client.post(route, headers=HEADERS, json={"result": "too late"}).status_code == 409
        with app.state.store.connect() as db:
            assert db.execute("SELECT status FROM tasks").fetchone()[0] == "expired"
