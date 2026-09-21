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
    if "invalid_grant" in text:
        return (
            "Google refused the refresh token (invalid_grant). The usual cause is "
            "an OAuth consent screen still set to 'Testing', which expires refresh "
            "tokens after 7 days. Set it to 'In production' and re-run "
            "scripts/bootstrap_google_auth.py."
        )
    if "insufficient" in text.lower() and "scope" in text.lower():
        return (
            "Google token is missing a scope. Re-run scripts/bootstrap_google_auth.py "
            "to re-consent with the current scope list."
        )
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return f"Rate limited by the model provider: {text[:200]}"
    return f"{type(exc).__name__}: {text[:300]}"
