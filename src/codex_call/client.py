"""Authenticated owner/worker HTTP client with bounded external task polling."""

import asyncio
import time
from uuid import uuid4

import httpx


class APIError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"api_http_{status_code}")


class APIClient:
    def __init__(self, settings, worker=False, *, transport=None, client=None):
        token = settings.worker_token if worker else settings.api_token
        self._headers = {"Authorization": "Bearer " + token.get_secret_value()}
        self._client = client or httpx.AsyncClient(
            base_url=settings.api_base_url.rstrip("/"), timeout=10, transport=transport
        )
        self._owns_client = client is None

    async def request(self, method, path, body=None, idempotency_key=None):
        headers = dict(self._headers)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError:
            raise APIError(502) from None
        if response.is_error:
            raise APIError(response.status_code)
        return response.json()

    async def get_call(self, call_id):
        return await self.request("GET", f"/internal/calls/{call_id}")

    async def claim_call(self, call_id):
        return await self.request("POST", f"/internal/calls/{call_id}/claim")

    async def mark_cleanup(self, call_id):
        return await self.request("POST", f"/internal/calls/{call_id}/cleanup")

    async def set_status(self, call_id, status, error=None):
        return await self.request(
            "PATCH", f"/internal/calls/{call_id}", {"status": status, "error": error}
        )

    async def append_transcript(self, call_id, item):
        return await self.request("POST", f"/internal/calls/{call_id}/transcript", item)

    async def request_task(self, call_id, prompt, timeout_s=60):
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 4000 or not 5 <= timeout_s <= 120:
            raise ValueError("invalid_task_request")
        request_id = str(uuid4())
        path = f"/internal/calls/{call_id}/tasks"
        deadline = time.monotonic() + timeout_s
        try:
            async with asyncio.timeout(timeout_s):
                result = await self.request(
                    "POST",
                    path,
                    {"request_id": request_id, "prompt": prompt, "timeout_s": timeout_s},
                )
                while result["status"] not in {"completed", "expired", "cancelled"}:
                    result = await self.request("GET", path + "/" + request_id)
                    if result["status"] not in {"completed", "expired", "cancelled"}:
                        await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
                if result["status"] == "completed":
                    text = result.get("result")
                    if not isinstance(text, str) or len(text) > 12000:
                        raise ValueError("invalid_task_result")
                    return {"status": "completed", "result": text}
                return {"status": result["status"], "result": None}
        except TimeoutError:
            return {"status": "expired", "result": None}

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
