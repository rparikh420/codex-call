"""Validated service configuration. Secrets never have printable defaults."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODEX_CALL_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    api_token: SecretStr
    worker_token: SecretStr
    database_path: Path = Path("data/calls.sqlite3")
    api_base_url: str = "http://127.0.0.1:8000"
    mode: Literal["simulate", "live"] = "simulate"
    livekit_url: str | None = None
    livekit_key: SecretStr | None = None
    livekit_secret: SecretStr | None = None
    sip_trunk_id: str | None = None
    agent_name: str = "codex-call"
    max_duration_s: int = Field(default=1800, ge=30, le=1800)
    anthropic_model: str = "claude-haiku-4-5-20251001"
    deepgram_model: str = "flux-general-en"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    elevenlabs_voice_id: str | None = None
    event_callback_hosts: list[str] = Field(default_factory=list)
    events_enabled: bool = False
    mcp_allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("api_token", "worker_token")
    @classmethod
    def require_strong_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("bearer token must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def separate_roles(self):
        if self.api_token == self.worker_token:
            raise ValueError("API and worker bearer tokens must differ")
        return self
