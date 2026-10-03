# Setup and operation

## Fresh local install

Use Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.12 and uv 0.9.24 are the locally tested versions. Run the README's `uv sync --locked --python 3.12`, `.env` token-generation and storage-permission commands from the checkout. Generate separate owner and worker tokens of at least 32 characters. Do not put tokens in logs, screenshots or Git. Environment variables override `.env`; the latter is read relative to the process working directory.

The API defaults to loopback and simulation:

```sh
uv run codex-call-api --host 127.0.0.1 --port 8000
```

`GET /health` returns mode and process health without authentication. It does not prove worker readiness or provider connectivity. Owner calls use `CODEX_CALL_API_TOKEN`; internal worker updates use the distinct `CODEX_CALL_WORKER_TOKEN`. Both roles must be configured in this release, including for stdio settings validation.

Simulation queues records and moves them to a simulated answered state without a worker or provider request. End a simulated call explicitly with the hangup tool. Switching mode never changes an existing record's mode; reuse of the original idempotency key returns that original call. Restarting the API marks interrupted active calls failed with `interrupted_restart` and attempts cleanup rather than redialing.

## Codex stdio registration

Start the API separately. Replace this generic absolute path with your checkout, then run this command yourself if you want registration:

```sh
codex mcp add codex-call -- uv --directory /absolute/path/to/codex-call run codex-call-mcp
```

`uv --directory` sets the project working directory, so the service loads that checkout's `.env`. The installed Codex CLI accepts `NAME -- COMMAND...` and also `--env KEY=VALUE`; keep credentials in the protected `.env` rather than literal command arguments. This guide does not register anything automatically. Restart/reload your Codex session and discover the seven tools. Use `arguments: {"params": {...}}` when calling them. You can optionally install the packaged `skills/codex-call` folder using your normal skill setup; the service does not install it.

Use stdio for Codex. The hosted `/mcp` endpoint implements the modern `2026-07-28` HTTP contract, not SDK v1 streamable HTTP. Do not use `codex mcp add --url` with it unless your client explicitly supports that contract. A compatible ChatGPT host needs public HTTPS and an authorized gateway when it requires OAuth. No OAuth provider or marketplace publication is included. See [Events and hosted MCP](events.md).

## Configure live telephony

Live configuration can incur provider charges. Only use an authorized test recipient and task. This guide describes configuration; no accounts, trunks or calls have been provisioned or exercised by the project.

1. Use a paid Telnyx account, buy an appropriate voice number, and configure an outbound voice profile and credential-authenticated SIP connection. Set the caller ID to your authorized number. Follow the [Telnyx SIP guide](https://developers.telnyx.com/docs/voice/sip-trunking/get-started) and [LiveKit Telnyx setup](https://docs.livekit.io/telephony/start/providers/telnyx/) for current provider steps. Select your operating region and provider-supported codecs/transport.
2. In your own LiveKit project, create an [outbound SIP trunk](https://docs.livekit.io/telephony/making-calls/outbound-trunk/) pointing to `sip.telnyx.com`, using that SIP username, password and caller number. This generic JSON is trunk configuration, not a command executed by Codex Call:

```json
{
  "name": "Operator outbound trunk",
  "address": "sip.telnyx.com",
  "numbers": ["+14155550123"],
  "authUsername": "your-sip-username",
  "authPassword": "your-sip-password",
  "headers": {"X-Telnyx-Username": "your-sip-username"}
}
```

`headers` supplies outgoing SIP headers in the installed LiveKit API. `headers_to_attributes` maps received headers to participant attributes and is not a substitute. Check your provider's current authentication requirements. Configure region using your own project's regional telephony settings; do not reuse someone else's endpoint or number.

3. In `.env`, explicitly set `CODEX_CALL_MODE=live` and fill the commented `CODEX_CALL_LIVEKIT_URL`, `CODEX_CALL_LIVEKIT_KEY`, `CODEX_CALL_LIVEKIT_SECRET` and `CODEX_CALL_SIP_TRUNK_ID`. The API uses these for dispatch and cleanup. Fill `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` and `ELEVEN_API_KEY` for the voice worker. These three keys must be in the worker's environment, not just parsed as application settings.
4. Stop the simulation API and restart one API process with the live settings. In another terminal from the same checkout, start the separate worker:

```sh
uv run --env-file .env codex-call-worker start
```

The `--env-file` option exports provider keys into the worker process. The worker's `CODEX_CALL_API_BASE_URL` must address the running API; it defaults to `http://127.0.0.1:8000`. Its named dispatch entrypoint is `codex-call`; if changing `CODEX_CALL_AGENT_NAME`, use the same value in both processes.

Configured defaults are Haiku `claude-haiku-4-5-20251001`, Deepgram `flux-general-en` and ElevenLabs `eleven_turbo_v2_5`; account access is unverified. You can set the model and voice fields in `.env.example`. Flux uses STT turn detection and IVR handling; non-Flux uses Silero VAD and may need model assets in the SDK cache. The worker retains the installed LiveKit CLI commands; inspect `codex-call-worker --help` and `codex-call-worker start --help` without credentials. Only `start` is documented for phone operation here. Console recording modes are outside this release's phone workflow.

5. Place one explicitly authorized call and inspect final status, transcript, actual received audio and room cleanup. Verify provider billing, caller ID, DTMF on your intended IVR and hold handling. Until those are checked, offline tests do not establish live readiness.

## Docker Compose

The Dockerfile uses Python 3.12, uv 0.9.24, locked runtime dependencies, and system `libportaudio2` for SDK CLI imports. It copies only package files and runs as UID 10001. `.dockerignore` keeps `.env`, SQLite, local workflow files and caches out of the build context. See [uv's Docker guide](https://docs.astral.sh/uv/guides/integration/docker/) for the underlying packaging approach.

After generating `.env` and leaving simulation enabled, run:

```sh
docker compose up --build api
```

This starts only the simulation API on host loopback port 8000 and persists its database in the `call-data` volume at `/data`. It does not require a worker. To use live operation, finish the telephony/provider setup, explicitly set `CODEX_CALL_MODE=live` in `.env`, and opt into the live profile:

```sh
docker compose --profile live up --build
```

The worker addresses the API at `http://api:8000`, using Compose service DNS. Its SDK health/API port 8081 is internal and not published to the host. `/health` on the API is not worker readiness; inspect worker connectivity before calling. Containers need provider network access. The image and Compose topology have not been executed locally because a Docker daemon was unavailable; CI checks Python behavior on Linux, not container execution. Do not treat these files as verified deployment evidence.

## Storage and recovery

Run exactly one API process and scheduler per database; do not use multiple Uvicorn workers or API replicas. Startup recovery fails all interrupted active calls and tries to delete their rooms. Failed cleanup is persisted for watchdog retries. Neither restart recovery nor cleanup guarantees multi-instance ownership or HA.

Use a private directory (`chmod 700 data`), a restrictive process umask (`umask 077`), and a protected `.env` (`chmod 600 .env`). Existing SQLite and journal/WAL files should be readable only by the operator (`chmod 600 data/*.sqlite*` when present). For Compose, protect the Docker host and volume backups; use the container's UID 10001 for bind mounts if replacing the named volume. The image creates `/data` with mode 700.

Call destinations, briefings, transcripts, host task results and event secrets persist until you remove the database and backups. There is no automatic retention deletion or per-call purge endpoint. To erase local data, first end active calls and stop API/worker processes, then remove the database and associated SQLite journal/WAL files. For Compose, removing the data volume also erases all history; do it only when that deletion is intended. Deleting local data does not delete provider-side billing or logs. No phone recordings are created by the application's session (`record=False`); separately configured provider recording is the operator's responsibility.

Application credential values are excluded from its configured error messages and dispatch metadata. Keep provider SDK debug logging and HTTP traces off when operating with secrets or personal data; review operator log collection separately.

## Dependency licensing

The published source, examples and calling skill are original MIT content. Runtime dependencies are referenced in `pyproject.toml`/`uv.lock`, not vendored into the source distribution. Installed metadata was checked for the locked environment: the direct packages use MIT, BSD and Apache licenses, while transitive packages additionally include PSF, ISC and MPL notices. Preserve each dependency's notices when redistributing its files.

[`livekit-local-inference` 0.2.7](https://pypi.org/project/livekit-local-inference/0.2.7/) declares `Apache-2.0 AND LicenseRef-LiveKit-Model`. Its installed package includes `livekit_local_inference-0.2.7.dist-info/licenses/MODEL_LICENSE`, which limits model use to the LiveKit Agents framework and includes redistribution terms. This project uses that framework and publishes source only, with no dependency wheels or model files. PyAV and other binary wheels may also include separately licensed native components. The MIT license here does not relicense dependencies, their models or native components. Provider service terms and model account permissions apply separately.
