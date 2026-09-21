"""Failure behaviour of the LLM layer.

These pin the part that actually decides whether a brief arrives. A provider
outage is not hypothetical: on the first scheduled run, Gemini returned 503 on
every model for several minutes, the SDK's internal retries stretched a single
request past two minutes, and the job hit its timeout and delivered nothing.

The rule these encode: degrade, never hang.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

import brief.llm as llm
from brief.config import Settings


class Dummy(BaseModel):
    x: str


def settings() -> Settings:
    return Settings(gemini_api_key="test-key", llm_triage_provider="gemini")


def install_fake_client(monkeypatch, behaviour):
    """Replace genai.Client with one whose generate_content does `behaviour`."""
    from google import genai

    class FakeModels:
        async def generate_content(self, **kwargs):
            return await behaviour(kwargs)

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        def __init__(self, **kwargs):
            self.aio = FakeAio()

    monkeypatch.setattr(genai, "Client", FakeClient)


async def test_a_hanging_provider_hits_the_deadline_and_degrades(monkeypatch):
    """The caller must get a prompt failure, not an unbounded wait."""
    monkeypatch.setattr(llm, "_TIER_DEADLINE_SECONDS", 2)
    monkeypatch.setattr(llm, "_RETRY_DELAYS", (0, 60, 60))

    async def hang(_kwargs):
        await asyncio.sleep(999)

    install_fake_client(monkeypatch, hang)

    started = time.monotonic()
    with pytest.raises(llm.LLMUnavailable, match="did not answer within"):
        await llm._gemini(settings(), "gemini-3.8-flash", "sys", "prompt", Dummy)
    elapsed = time.monotonic() - started

    assert elapsed < 6, f"deadline did not fire promptly ({elapsed:.1f}s)"


async def test_503_on_every_model_raises_rather_than_retrying_forever(monkeypatch):
    monkeypatch.setattr(llm, "_RETRY_DELAYS", (0, 0))
    attempts = []

    async def always_503(kwargs):
        attempts.append(kwargs["model"])
        raise RuntimeError("503 UNAVAILABLE. The model is overloaded.")

    install_fake_client(monkeypatch, always_503)

    with pytest.raises(llm.LLMUnavailable, match="every gemini model failed"):
        await llm._gemini(settings(), "gemini-3.8-flash", "sys", "prompt", Dummy)

    # Two rounds over a three-model chain.
    assert len(attempts) == 6
    assert attempts[0] == "gemini-3.8-flash"


async def test_a_retired_model_falls_through_to_a_live_one(monkeypatch):
    """404 means retired - hop immediately, don't wait."""
    monkeypatch.setattr(llm, "_RETRY_DELAYS", (0,))

    class Result:
        parsed = Dummy(x="ok")

    async def dead_primary(kwargs):
        if kwargs["model"] == "gemini-3.8-flash":
            raise RuntimeError("404 NOT_FOUND. This model is not available.")
        return Result()

    install_fake_client(monkeypatch, dead_primary)

    out = await llm._gemini(settings(), "gemini-3.8-flash", "sys", "prompt", Dummy)
    assert out.x == "ok"


async def test_a_real_error_is_not_swallowed_as_transient(monkeypatch):
    """A bad API key must surface immediately, not burn the retry budget."""
    monkeypatch.setattr(llm, "_RETRY_DELAYS", (0, 60))
    calls = []

    async def bad_key(kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("400 INVALID_ARGUMENT. API key not valid.")

    install_fake_client(monkeypatch, bad_key)

    with pytest.raises(RuntimeError, match="API key not valid"):
        await llm._gemini(settings(), "gemini-3.8-flash", "sys", "prompt", Dummy)

    assert len(calls) == 1, "a non-transient error must not be retried"


async def test_disabled_tier_reports_itself_clearly():
    s = Settings(llm_triage_provider="none")
    with pytest.raises(llm.LLMUnavailable, match="disabled"):
        await llm.complete(
            settings=s, system="s", prompt="p", schema=Dummy, tier="triage"
        )
