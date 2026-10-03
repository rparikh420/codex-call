"""Bounded HTTPS delivery using only public DNS answers and the original TLS hostname."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import re
import time
from urllib.parse import urlsplit

import aiohttp


class DestinationRejected(ValueError):
    pass


def decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("invalid_webhook_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise ValueError("invalid_webhook_secret") from None
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_webhook_secret")
    return key


def sign(event_id: str, body: bytes, secrets: list[str], timestamp: int | None = None) -> dict:
    timestamp = int(time.time()) if timestamp is None else timestamp
    message = f"{event_id}.{timestamp}.".encode() + body
    signatures = [
        "v1,"
        + base64.b64encode(
            hmac.new(decode_secret(secret), message, hashlib.sha256).digest()
        ).decode()
        for secret in secrets
    ]
    return {
        "webhook-id": event_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": " ".join(signatures),
        "Content-Type": "application/json",
    }


def validate_url(url: str, hosts: list[str]) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or "\\" in url
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
            or host not in hosts
            or parts.port == 0
            or not re.fullmatch(
                r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}", host
            )
        ):
            raise DestinationRejected("destination_rejected")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host
    except (ValueError, TypeError):
        pass
    raise DestinationRejected("destination_rejected")


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        answers = await self.resolver.resolve(host, port, family)
        if not answers:
            raise DestinationRejected("destination_rejected")
        for answer in answers:
            address = ipaddress.ip_address(answer["host"])
            if (
                not address.is_global
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
                or address.is_loopback
                or address.is_link_local
            ):
                raise DestinationRejected("destination_rejected")
        return answers

    async def close(self):
        await self.resolver.close()


class WebhookSender:
    async def post(self, url, body, headers, hosts, *, acknowledgement_only=False):
        validate_url(url, hosts)
        if len(body) > 262144:
            raise ValueError("payload_too_large")
        resolver = PublicResolver()
        try:
            connector = aiohttp.TCPConnector(
                resolver=resolver, use_dns_cache=False, force_close=True
            )
            async with aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=5),
                auto_decompress=False,
            ) as session:
                # aiohttp connects to resolver answers while retaining URL host for TLS SNI.
                async with session.post(
                    url, data=body, headers=headers, allow_redirects=False
                ) as response:
                    if acknowledgement_only:
                        # A delivery ACK is the status alone. Close immediately so body
                        # errors or unbounded content cannot change the retry decision.
                        response.close()
                        return response.status, b""
                    try:
                        content = await response.content.readexactly(65537)
                    except asyncio.IncompleteReadError as exc:
                        content = exc.partial
                    if len(content) > 65536:
                        raise ValueError("response_too_large")
                    return response.status, content
        finally:
            await resolver.close()
