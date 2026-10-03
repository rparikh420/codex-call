"""Independent call contracts used at the HTTP and persistence boundaries."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CallStatus = Literal[
    "queued", "dispatching", "dialing", "answered", "completed", "failed", "cancelled"
]
ErrorCode = Literal[
    "dispatch_failed",
    "cleanup_failed",
    "interrupted_restart",
    "provider_failed",
    "duration_exceeded",
    "cancelled",
]
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CallRequest(StrictModel):
    to_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    task: str = Field(min_length=1, max_length=12000)
    personality: str = Field(default="", max_length=2000)
    max_duration_s: int = Field(default=600, ge=30, le=1800)


class CallUpdate(StrictModel):
    status: CallStatus
    error: ErrorCode | None = None


class TranscriptItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=24000)


class TaskRequest(StrictModel):
    request_id: UUID
    prompt: str = Field(min_length=1, max_length=4000)
    timeout_s: int = Field(default=60, ge=5, le=120)


class TaskResult(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    result: str = Field(min_length=1, max_length=12000)
