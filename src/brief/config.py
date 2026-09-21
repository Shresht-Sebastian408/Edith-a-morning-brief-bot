"""Configuration, loaded from environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Gmail's IMAP endpoint. Reads use BODY.PEEK, which does not set the \Seen
# flag, so nothing in your inbox is marked as read by this system. Nothing is
# ever sent, deleted or moved either - but note that an app password grants
# more than that, so it is a credential to protect like a password.
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

Provider = Literal["anthropic", "gemini", "none"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gmail_address: str = ""
    gmail_app_password: str = ""
    calendar_ical_url: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    llm_synthesis_provider: Provider = "gemini"
    llm_triage_provider: Provider = "gemini"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    timezone: str = "Asia/Kolkata"
    lookback_hours: int = Field(default=24, ge=1, le=168)

    # Model ids. Pinned rather than inferred so a provider's "latest" alias
    # can't silently change the brief's voice overnight.
    anthropic_synthesis_model: str = "claude-opus-5"
    anthropic_triage_model: str = "claude-haiku-4-5"
    # 3.5-flash leads because it is the one that actually answers. Measured
    # 2026-09-21 across two API keys in different projects: 3.8-flash (the
    # flagship) returned 503 on every single call, 3.5-flash returned 200 on
    # every call, and 2.5-flash is retired and 404s. The chain still ends at
    # 3.8 so it gets used again once Google's capacity recovers.
    gemini_synthesis_model: str = "gemini-3.5-flash"
    gemini_triage_model: str = "gemini-3.5-flash"
    gemini_fallback_models: str = "gemini-3.6-flash,gemini-3.8-flash"

    @property
    def gemini_chain(self) -> list[str]:
        return [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]

    @field_validator("gmail_app_password", mode="after")
    @classmethod
    def _strip_app_password(cls, value: str) -> str:
        """Google displays app passwords in four spaced groups.

        People paste them exactly as shown, and IMAP LOGIN rejects the spaces
        with an unhelpful AUTHENTICATIONFAILED, so normalise here rather than
        make the user notice.
        """
        return "".join(value.split())

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def require(self, *fields: str) -> None:
        """Fail loudly at startup rather than cryptically mid-run."""
        missing = [f for f in fields if not getattr(self, f, None)]
        if missing:
            raise RuntimeError(
                "Missing required configuration: "
                + ", ".join(sorted(m.upper() for m in missing))
                + ". See .env.example."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
