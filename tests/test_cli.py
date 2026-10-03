"""Exercise installed entry points at the process and HTTP boundaries."""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest


def cli_env():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_CALL_", "LIVEKIT_", "ANTHROPIC_", "DEEPGRAM_", "ELEVEN_"))
    }


@pytest.mark.parametrize(
    "command,args,expected",
    [
        ("codex-call-api", ["--help"], "--host"),
        ("codex-call-worker", ["--help"], "start"),
        ("codex-call-worker", ["start", "--help"], "--drain-timeout"),
        ("codex-call-mcp", ["--help"], "stdio"),
    ],
)
def test_help_without_credentials_has_no_runtime_side_effects(tmp_path, command, args, expected):
    executable = shutil.which(command)
    assert executable
    result = subprocess.run(
        [executable, *args], cwd=tmp_path, env=cli_env(), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_worker_start_still_requires_credentials(tmp_path):
    result = subprocess.run(
        [shutil.which("codex-call-worker"), "start"],
        cwd=tmp_path,
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "api_token" in result.stderr and "worker_token" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_api_cli_host_and_port_serve_authenticated_simulation(tmp_path):
    # The test breaks if CLI flags are ignored or runtime auth/simulation changes.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = cli_env() | {
        "CODEX_CALL_API_TOKEN": "test-owner-" + "a" * 32,
        "CODEX_CALL_WORKER_TOKEN": "test-worker-" + "b" * 32,
    }
    process = subprocess.Popen(
        [shutil.which("codex-call-api"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(process.stderr.read())
                try:
                    response = client.get("/v1/calls")
                    break
                except httpx.ConnectError:
                    time.sleep(0.02)
            else:
                pytest.fail("API CLI did not become reachable at requested address")
            assert response.status_code == 401
            response = client.post(
                "/v1/calls",
                headers={
                    "Authorization": f"Bearer {env['CODEX_CALL_API_TOKEN']}",
                    "Idempotency-Key": "cli-simulation",
                },
                json={"to_number": "+14155550123", "task": "Ask when the shop opens"},
            )
            assert response.status_code == 201
            assert response.json()["mode"] == "simulate"
            assert Path(tmp_path / "data/calls.sqlite3").is_file()
    finally:
        process.terminate()
        process.communicate(timeout=10)
