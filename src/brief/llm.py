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

import logging
from typing import Literal, TypeVar

from pydantic import BaseModel

from brief.config import Provider, Settings

log = logging.getLogger(__name__)

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
    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )

    # The flagship returns 503 under load, and models get retired without
    # warning. Neither should cost you the morning, so walk a chain.
    candidates = [model] + [m for m in settings.gemini_chain if m != model]
    response = None
    failures: list[str] = []

    for candidate in candidates:
        try:
            response = await client.aio.models.generate_content(
                model=candidate, contents=prompt, config=config
            )
            if candidate != model:
                log.warning("gemini fell back to %s", candidate)
            break
        except Exception as exc:  # noqa: BLE001 - provider raises several types
            text = str(exc)
            transient = any(code in text for code in ("503", "429", "404", "UNAVAILABLE"))
            failures.append(f"{candidate}: {text[:120]}")
            log.warning("gemini %s failed: %s", candidate, text[:160])
            if not transient:
                raise

    if response is None:
        raise LLMUnavailable("every gemini model failed - " + " | ".join(failures))

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
