"""Provider-agnostic structured LLM calls.

Every LLM call in this system goes through `complete()`. Callers pass a Pydantic
schema and get back a validated instance; nobody parses JSON by hand, and no
agent knows which vendor it is talking to.

Two tiers, because the two jobs have genuinely different requirements:

    triage     - high volume, low judgment. Runs once per source per morning.
    synthesis  - one call, writes the brief you actually hear. Quality is felt.

Both default to Gemini. Set LLM_SYNTHESIS_PROVIDER=anthropic to route just the
final brief through Claude while triage stays on Gemini's free tier.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, TypeVar

from pydantic import BaseModel

from brief.config import Provider, Settings

log = logging.getLogger(__name__)

# Spacing between full passes over the model chain. The first pass is
# immediate; later ones give a saturated backend time to recover.
_RETRY_DELAYS = (0, 6, 20)
# Ceiling on one request. The SDK otherwise retries internally for minutes.
_REQUEST_TIMEOUT_MS = 25_000
# Ceiling on one tier, retries included. Past this the caller degrades to a
# rules-only or mechanically assembled brief rather than missing the morning.
_TIER_DEADLINE_SECONDS = 100

T = TypeVar("T", bound=BaseModel)
Tier = Literal["triage", "synthesis"]


class LLMUnavailable(RuntimeError):
    """Raised when a tier has no usable provider. Callers degrade, not crash."""


def _resolve(settings: Settings, tier: Tier) -> tuple[Provider, str]:
    if tier == "triage":
        provider = settings.llm_triage_provider
        model = (
            settings.gemini_triage_model
            if provider == "gemini"
            else settings.anthropic_triage_model
        )
    else:
        provider = settings.llm_synthesis_provider
        model = (
            settings.gemini_synthesis_model
            if provider == "gemini"
            else settings.anthropic_synthesis_model
        )
    return provider, model


async def complete(
    *,
    settings: Settings,
    system: str,
    prompt: str,
    schema: type[T],
    tier: Tier,
) -> T:
    """Run one structured completion and return a validated `schema` instance."""
    provider, model = _resolve(settings, tier)

    if provider == "none":
        raise LLMUnavailable(f"tier '{tier}' is disabled (provider=none)")

    log.info("llm[%s] provider=%s model=%s", tier, provider, model)

    if provider == "gemini":
        return await _gemini(settings, model, system, prompt, schema)
    return await _anthropic(settings, model, system, prompt, schema)


async def _gemini(
    settings: Settings, model: str, system: str, prompt: str, schema: type[T]
) -> T:
    from google import genai
    from google.genai import types

    settings.require("gemini_api_key")
    # Without an explicit timeout the SDK runs its own internal retry-and-backoff
    # inside a single call. Observed in production: one request sat for 2m07s
    # before surfacing a 503, and nine of those overran the job timeout entirely.
    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
    )
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )

    # Two independent failure modes, needing two different remedies:
    #
    #   404  a model was retired - hop to another model, immediately.
    #   503  the backend is saturated - hopping does not help, because the
    #        models share it. Waiting does; Google's own message says spikes
    #        are usually temporary.
    #
    # So each round walks the whole chain, and rounds are spaced out. A 7am
    # cron can afford to wait half a minute; it cannot afford to skip a day.
    candidates = [model] + [m for m in settings.gemini_chain if m != model]
    failures: list[str] = []

    async def walk_chain():
        for round_index, delay in enumerate(_RETRY_DELAYS):
            if delay:
                log.info("gemini saturated, waiting %ss before retry %d", delay, round_index)
                await asyncio.sleep(delay)

            for candidate in candidates:
                try:
                    result = await client.aio.models.generate_content(
                        model=candidate, contents=prompt, config=config
                    )
                    if candidate != model or round_index:
                        log.warning(
                            "gemini succeeded on %s (round %d)", candidate, round_index
                        )
                    return result
                except Exception as exc:  # noqa: BLE001 - provider raises several types
                    text = str(exc)
                    transient = any(
                        c in text
                        for c in (
                            "500", "502", "503", "504", "429",
                            "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                            "DEADLINE_EXCEEDED", "INTERNAL", "timeout",
                        )
                    )
                    retired = "404" in text or "NOT_FOUND" in text
                    failures.append(f"{candidate}: {text[:90]}")
                    log.warning("gemini %s failed: %s", candidate, text[:130])
                    if not (transient or retired):
                        raise
        return None

    # A hard ceiling on the whole tier. During a sustained provider outage the
    # retries above are worth having, but only up to a point: a brief that
    # arrives at 07:01 written by rules beats one that never arrives because
    # the job timed out at 07:12. Past the deadline the caller degrades.
    try:
        response = await asyncio.wait_for(walk_chain(), timeout=_TIER_DEADLINE_SECONDS)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise LLMUnavailable(
            f"gemini did not answer within {_TIER_DEADLINE_SECONDS}s "
            f"({len(failures)} attempts failed) - degrading"
        ) from exc

    if response is None:
        raise LLMUnavailable(
            f"every gemini model failed across {len(_RETRY_DELAYS)} rounds - "
            + " | ".join(failures[-2:])
        )

    parsed = response.parsed
    if parsed is None:
        # Schema-constrained decoding failed, usually a safety block or a
        # truncated response. Surface the text so the failure is diagnosable.
        raise LLMUnavailable(
            f"gemini returned no parsable output for {schema.__name__}: "
            f"{(response.text or '')[:300]!r}"
        )
    if isinstance(parsed, schema):
        return parsed
    return schema.model_validate(parsed)


async def _anthropic(
    settings: Settings, model: str, system: str, prompt: str, schema: type[T]
) -> T:
    import anthropic

    settings.require("anthropic_api_key")
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.parse(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )

    # Safety classifiers can decline with HTTP 200, so stop_reason is checked
    # before content is read.
    if response.stop_reason == "refusal":
        raise LLMUnavailable(f"anthropic refused the request: {response.stop_details}")

    parsed = response.parsed_output
    if parsed is None:
        raise LLMUnavailable(f"anthropic returned no parsable {schema.__name__}")
    return parsed
