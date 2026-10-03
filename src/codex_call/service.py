"""Durable scheduler and bounded external-provider cleanup."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from codex_call.models import TERMINAL_STATES
from codex_call.store import Conflict, Store


class CleanupFailed(Exception):
    pass


class SimulationProvider:
    """Explicit local simulation: no SDK or network operations."""

    async def dispatch(self, call):
        if call["mode"] != "simulate":
            raise ValueError("call_mode_mismatch")
        return {"status": "answered"}

    async def hangup(self, call):
        if call["mode"] != "simulate":
            raise ValueError("call_mode_mismatch")
        return None


class CallService:
    dispatch_timeout_s = 30
    cleanup_timeout_s = 10
    max_concurrent_dispatches = 8

    def __init__(self, store: Store, provider):
        self.store = store
        self.provider = provider
        self._wake = asyncio.Event()
        self._dispatches: dict[str, asyncio.Task] = {}
        self._scheduler: asyncio.Task | None = None

    async def start(self):
        # Mark interrupted calls before any external action: they can never be redialled.
        for call in self.store.active_calls():
            self.store.transition(call["id"], "failed", "interrupted_restart")
            with suppress(CleanupFailed):
                await self.cleanup(call)
        self._scheduler = asyncio.create_task(self._run())

    async def stop(self):
        if self._scheduler is not None:
            self._scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await self._scheduler
        tasks = list(self._dispatches.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self.provider, "aclose"):
            await self.provider.aclose()

    def notify(self):
        self._wake.set()

    async def _run(self):
        while True:
            self._wake.clear()
            await self.watchdog_once(datetime.now(UTC))
            while len(self._dispatches) < self.max_concurrent_dispatches:
                call = self.store.claim_queued()
                if call is None:
                    break
                task = asyncio.create_task(self._dispatch(call))
                self._dispatches[call["id"]] = task
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.1)
            except TimeoutError:
                pass

    async def watchdog_once(self, timestamp):
        for call in self.store.expire_calls(timestamp):
            task = self._dispatches.get(call["id"])
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            with suppress(CleanupFailed):
                await self.cleanup(call)

    async def _dispatch(self, call):
        try:
            if self.store.get_call(call["id"])["status"] in TERMINAL_STATES:
                return
            result = await asyncio.wait_for(self.provider.dispatch(call), self.dispatch_timeout_s)
            if (
                type(self.provider) is SimulationProvider
                and isinstance(result, dict)
                and result.get("status")
            ):
                with suppress(Conflict):
                    self.store.transition(call["id"], result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(Conflict):
                self.store.transition(call["id"], "failed", "dispatch_failed")
            with suppress(CleanupFailed):
                await self.cleanup(call)
        finally:
            self._dispatches.pop(call["id"], None)
            self.notify()

    async def cleanup(self, call):
        try:
            await asyncio.wait_for(self.provider.hangup(call), self.cleanup_timeout_s)
            self.store.clear_cleanup(call["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.mark_cleanup(call["id"])
            raise CleanupFailed("cleanup_failed") from None

    async def hangup(self, call_id):
        call = self.store.get_call(call_id)
        if call["status"] not in TERMINAL_STATES:
            with suppress(Conflict):
                self.store.transition(call_id, "cancelled")
            call = self.store.get_call(call_id)
        task = self._dispatches.get(call_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # Always retry cleanup, including already-terminal calls.
        await self.cleanup(call)
        return self.store.get_call(call_id)
