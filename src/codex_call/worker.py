"""Named LiveKit worker with single-owner dialing and bounded cleanup."""

import asyncio
import json
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from inspect import isawaitable
from uuid import UUID

from livekit.agents import AgentServer, cli, room_io

from codex_call.client import APIClient, APIError
from codex_call.config import Settings
from codex_call.provider import LiveKitProvider, validate_livekit
from codex_call.voice import create_session, make_agent


async def run_call(ctx, settings, *, client=None, provider=None, session_factory=create_session):
    client = client or APIClient(settings, worker=True)
    call = None
    eligible = False
    claimed = False
    session = None
    consumer = None
    queue = asyncio.Queue()
    done = asyncio.Event()
    failure = None
    disconnected = None

    async def transcript_consumer():
        nonlocal failure
        while (item := await queue.get()) is not None:
            try:
                await client.append_transcript(call["id"], item)
            except Exception:
                failure = "provider_failed"
                done.set()

    def transcript(event):
        item = event.item
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        if role in {"user", "assistant"} and isinstance(text, str) and text.strip():
            queue.put_nowait({"item_id": item.id, "role": role, "text": text})

    def closed(event):
        nonlocal failure
        if event.error is not None:
            failure = "provider_failed"
        done.set()

    def error(event):
        nonlocal failure
        failure = "provider_failed"
        done.set()

    async def status(value, code=None):
        try:
            await client.set_status(call["id"], value, code)
        except APIError as exc:
            if exc.status_code != 409:
                raise

    try:
        try:
            metadata = json.loads(ctx.job.metadata)
            call_id = str(UUID(metadata["call_id"]))
        except (ValueError, TypeError, KeyError):
            return
        async with asyncio.timeout(settings.max_duration_s):
            call = await client.get_call(call_id)
            if (
                call["room_name"] != ctx.room.name
                or call["status"] != "dispatching"
                or call["mode"] != "live"
            ):
                return
            eligible = True
            provider = provider or LiveKitProvider(settings)
            candidate = session_factory(settings)  # Credentials fail before SIP creation.
            session = await candidate if isawaitable(candidate) else candidate
            try:
                call = await client.claim_call(call_id)
            except APIError as exc:
                if exc.status_code == 409:
                    eligible = False  # Another worker or cancellation owns the record.
                    return
                raise
            claimed = True
            remaining = min(
                call["max_duration_s"],
                settings.max_duration_s,
                (datetime.fromisoformat(call["deadline_at"]) - datetime.now(UTC)).total_seconds(),
            )
            deadline = time.monotonic() + max(0, remaining)
            async with asyncio.timeout(max(0, remaining)):

                def disconnected(*_):
                    done.set()

                ctx.room.on("disconnected", disconnected)
                session.on("conversation_item_added", transcript)
                session.on("close", closed)
                session.on("error", error)
                consumer = asyncio.create_task(transcript_consumer())
                await ctx.connect()
                await session.start(
                    agent=make_agent(ctx, call, client, done, deadline),
                    room=ctx.room,
                    record=False,
                    room_options=room_io.RoomOptions(
                        participant_identity="phone-" + call_id, close_on_disconnect=True
                    ),
                )
                # Connect/start may take time; recheck cancellation immediately before dialing.
                current = await client.get_call(call_id)
                if current["status"] != "dialing" or done.is_set():
                    return
                await provider.dial(call, deadline - time.monotonic())
                current = await client.get_call(call_id)
                if current["status"] != "dialing" or done.is_set():
                    return
                await client.set_status(call_id, "answered")
                session.generate_reply(
                    instructions="Introduce yourself as an AI assistant and "
                    "begin the caller task when the phone is answered."
                )
                await done.wait()
    except TimeoutError:
        failure = "duration_exceeded"
    except asyncio.CancelledError:
        failure = "provider_failed"
        raise
    except Exception:
        failure = "provider_failed"
    finally:
        if session is not None:
            session.shutdown()
            try:
                await asyncio.wait_for(session.aclose(), 5)
            except Exception:
                failure = failure or "provider_failed"
        if consumer is not None:
            queue.put_nowait(None)
            try:
                await asyncio.wait_for(consumer, 5)
            except Exception:
                failure = failure or "provider_failed"
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
        if disconnected is not None:
            ctx.room.off("disconnected", disconnected)
        if provider is not None:
            if claimed:
                try:
                    await asyncio.wait_for(provider.hangup(call), 10)
                except Exception:
                    failure = failure or "cleanup_failed"
                    with suppress(Exception):
                        await client.mark_cleanup(call["id"])
            with suppress(Exception):
                await provider.aclose()
        if call is not None and eligible:
            with suppress(Exception):
                await status("failed" if failure else "completed", failure)
        await client.aclose()


def build_server(settings):
    validate_livekit(settings)
    server = AgentServer(
        ws_url=settings.livekit_url,
        api_key=settings.livekit_key.get_secret_value(),
        api_secret=settings.livekit_secret.get_secret_value(),
    )

    @server.rtc_session(agent_name=settings.agent_name)
    async def entrypoint(ctx):
        await run_call(ctx, settings)

    return server


def main():
    # SDK help only inspects commands; no configured server or network is needed.
    # Every operational command still constructs and validates the real settings.
    server = AgentServer() if "--help" in sys.argv[1:] else build_server(Settings())
    cli.run_app(server)
