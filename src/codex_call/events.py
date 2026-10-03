"""Verified subscriptions and a transactional, at-least-once webhook outbox."""

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import aiohttp
from pydantic import Field, ValidationError

from codex_call.models import StrictModel
from codex_call.store import NotFound, now
from codex_call.webhooks import (
    DestinationRejected,
    WebhookSender,
    decode_secret,
    sign,
    validate_url,
)


class EventError(Exception):
    def __init__(self, code=-32602, reason="invalid_arguments"):
        self.code = code
        self.reason = reason
        super().__init__(reason)


class EventArguments(StrictModel):
    call_id: UUID | None = None


class Delivery(StrictModel):
    mode: Literal["webhook"]
    url: str = Field(max_length=2048)
    secret: str | None = Field(default=None, max_length=100)


class Subscription(StrictModel):
    name: Literal["call.task_requested"]
    arguments: EventArguments = Field(default_factory=EventArguments)
    delivery: Delivery
    cursor: None = None
    ttlMs: int = Field(default=86400000, ge=1, le=86400000, strict=True)


class EventManager:
    def __init__(self, store, settings, *, sender=None):
        self.store = store
        self.settings = settings
        self.owner = hashlib.sha256(settings.api_token.get_secret_value().encode()).hexdigest()
        self.sender = sender or WebhookSender()
        self.lock = asyncio.Lock()

    def _parse(self, params, *, verifying=True):
        if not self.settings.events_enabled:
            raise EventError(-32601, "method_not_found")
        try:
            subscription = Subscription.model_validate(params)
            if verifying:
                decode_secret(subscription.delivery.secret)
                validate_url(subscription.delivery.url, self.settings.event_callback_hosts)
            arguments = subscription.arguments.model_dump(mode="json", exclude_none=True)
            if verifying and arguments.get("call_id"):
                self.store.get_call(arguments["call_id"])
        except DestinationRejected:
            raise EventError(-32015, "destination_rejected") from None
        except (ValidationError, ValueError, NotFound):
            raise EventError() from None
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        identity = json.dumps([self.owner, subscription.delivery.url, subscription.name, canonical])
        sub_id = hashlib.sha256(identity.encode()).hexdigest()
        return subscription, canonical, sub_id

    async def subscribe(self, params):
        subscription, arguments, sub_id = self._parse(params)
        async with self.lock:
            challenge = secrets.token_urlsafe(32)
            body = json.dumps(
                {"type": "verification", "challenge": challenge}, separators=(",", ":")
            ).encode()
            headers = sign(str(uuid4()), body, [subscription.delivery.secret])
            headers["X-MCP-Subscription-Id"] = sub_id
            try:
                async with asyncio.timeout(5):
                    status, response = await self.sender.post(
                        subscription.delivery.url, body, headers, self.settings.event_callback_hosts
                    )
                echoed = json.loads(response).get("challenge")
                if (
                    not 200 <= status < 300
                    or not isinstance(echoed, str)
                    or not secrets.compare_digest(challenge.encode(), echoed.encode())
                ):
                    raise ValueError("challenge_failed")
            except TimeoutError:
                raise EventError(-32015, "timeout") from None
            except DestinationRejected:
                raise EventError(-32015, "destination_rejected") from None
            except (
                aiohttp.ClientError,
                OSError,
                ValueError,
                AttributeError,
                UnicodeError,
                RecursionError,
            ):
                raise EventError(-32015, "challenge_failed") from None
            expiration = (
                datetime.now(UTC) + timedelta(milliseconds=subscription.ttlMs)
            ).isoformat()
            with self.store.connect() as db:
                old = db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
                old_secret, rotate_until = None, None
                if old and old["expires_at"] > now():
                    if old["secret"] != subscription.delivery.secret:
                        old_secret = old["secret"]
                        rotate_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
                    else:
                        old_secret, rotate_until = old["old_secret"], old["rotate_until"]
                db.execute(
                    """INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,
                    old_secret=excluded.old_secret,
                    rotate_until=excluded.rotate_until,expires_at=excluded.expires_at""",
                    (
                        sub_id,
                        self.owner,
                        subscription.delivery.url,
                        subscription.name,
                        arguments,
                        subscription.delivery.secret,
                        old_secret,
                        rotate_until,
                        expiration,
                    ),
                )
            return {"id": sub_id, "refreshBefore": expiration, "cursor": None, "truncated": False}

    async def unsubscribe(self, params):
        _, _, sub_id = self._parse(params, verifying=False)
        async with self.lock:
            with self.store.connect() as db:
                db.execute("DELETE FROM subscriptions WHERE id=? AND owner=?", (sub_id, self.owner))
        return {}

    async def tick(self):
        if not self.settings.events_enabled:
            return
        async with self.lock:
            with self.store.connect() as db:
                self.store._expire_tasks(db)
                db.execute(
                    "DELETE FROM subscriptions WHERE expires_at<=? OR owner!=?", (now(), self.owner)
                )
                db.execute(
                    "UPDATE outbox SET status='failed' WHERE status='pending' AND attempts>=5"
                )
                rows = [
                    dict(row)
                    for row in db.execute(
                        """SELECT o.*,s.url,s.secret,s.old_secret,
                    s.rotate_until FROM outbox o JOIN subscriptions s ON s.id=o.subscription_id
                    WHERE o.status='pending' AND o.next_attempt_at<=?
                    ORDER BY o.next_attempt_at LIMIT 20""",
                        (now(),),
                    )
                ]
            for row in rows:
                # Recheck immediately before sending: earlier deliveries may have taken time.
                with self.store.connect() as db:
                    self.store._expire_tasks(db)
                    valid = db.execute(
                        """SELECT 1 FROM outbox o JOIN subscriptions s ON s.id=o.subscription_id
                        WHERE o.subscription_id=? AND o.event_id=? AND o.status='pending'
                        AND s.expires_at>? AND s.owner=?""",
                        (row["subscription_id"], row["event_id"], now(), self.owner),
                    ).fetchone()
                if not valid:
                    continue
                attempts = row["attempts"] + 1
                next_attempt = (
                    datetime.now(UTC) + timedelta(seconds=min(300, 2**attempts))
                ).isoformat()
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE outbox SET attempts=?,next_attempt_at=? "
                        "WHERE subscription_id=? AND event_id=?",
                        (attempts, next_attempt, row["subscription_id"], row["event_id"]),
                    )
                keys = [row["secret"]]
                if row["old_secret"] and row["rotate_until"] > now():
                    keys.append(row["old_secret"])
                headers = sign(row["event_id"], row["body"], keys)
                headers["X-MCP-Subscription-Id"] = row["subscription_id"]
                try:
                    async with asyncio.timeout(5):
                        status, _ = await self.sender.post(
                            row["url"],
                            row["body"],
                            headers,
                            self.settings.event_callback_hosts,
                            acknowledgement_only=True,
                        )
                    retry = status in {408, 429} or 500 <= status < 600
                except (TimeoutError, aiohttp.ClientError, OSError):
                    status, retry = 0, True
                except (DestinationRejected, ValueError):
                    status, retry = 0, False
                state = (
                    "delivered"
                    if 200 <= status < 300
                    else ("pending" if retry and attempts < 5 else "failed")
                )
                next_attempt = (
                    datetime.now(UTC) + timedelta(seconds=min(300, 2**attempts))
                ).isoformat()
                with self.store.connect() as db:
                    db.execute(
                        """UPDATE outbox SET status=?,attempts=?,next_attempt_at=?
                        WHERE subscription_id=? AND event_id=?""",
                        (state, attempts, next_attempt, row["subscription_id"], row["event_id"]),
                    )

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(0.5)
