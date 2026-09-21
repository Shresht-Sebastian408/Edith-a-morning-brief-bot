"""The agent interface.

An agent owns exactly one noisy source and answers one question: "what, if
anything, from my source is worth this person's morning?" It returns an
`AgentReport` and never raises - see contracts.AgentReport for why.
"""

from __future__ import annotations

import logging
from typing import Protocol

from brief.config import Settings
from brief.contracts import AgentReport

log = logging.getLogger(__name__)


class Agent(Protocol):
    name: str

    async def run(self, settings: Settings) -> AgentReport: ...


async def run_safely(agent: Agent, settings: Settings) -> AgentReport:
    """Invoke an agent, converting any failure into a reportable error.

    One dead connector must not cost you the whole brief.
    """
    try:
        return await agent.run(settings)
    except Exception as exc:  # noqa: BLE001 - deliberate top-level containment
        message = _humanise(exc)
        log.warning("agent %s failed: %s", agent.name, message)
        # Traceback only when asked for it; a missing env var does not need one.
        log.debug("agent %s traceback", agent.name, exc_info=True)
        return AgentReport.failed(agent.name, message)


def _humanise(exc: Exception) -> str:
    """Turn common, cryptic failures into something actionable at 7am."""
    text = str(exc)
    lowered = text.lower()

    if "authenticationfailed" in lowered or "invalid credentials" in lowered:
        return (
            "Gmail rejected the login. Check GMAIL_APP_PASSWORD is the 16-character "
            "app password (spaces are fine) and not your normal account password, "
            "and that 2-Step Verification is still enabled - turning it off deletes "
            "every app password."
        )
    if "did not return a calendar feed" in lowered:
        return text
    if "404" in text and "ical" in lowered:
        return (
            "The calendar iCal URL returned 404. It was probably reset. Get a fresh "
            "secret address from Google Calendar settings."
        )
    if "429" in text or "resource_exhausted" in lowered:
        return f"Rate limited by the model provider: {text[:200]}"
    if "timed out" in lowered or "timeout" in lowered:
        return f"Network timeout reaching the source: {text[:200]}"
    return f"{type(exc).__name__}: {text[:300]}"
