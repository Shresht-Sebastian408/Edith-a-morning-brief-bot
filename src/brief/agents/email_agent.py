"""Email triage agent.

Three-stage funnel, cheapest filter first:

    1. Gmail query      - excludes promotions/social server-side. Free.
    2. Deterministic rules - drops known noise, promotes known priorities. Free.
    3. LLM judgment     - only the survivors, and only what rules can't settle.

Stage 3 is the only one that costs anything, which is why stages 1 and 2 are
aggressive. If the LLM is unavailable or disabled, the agent still produces a
usable report from rules alone - a degraded brief beats no brief.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, Field

from brief.config import Settings
from brief.connectors.gmail import EmailItem, fetch_recent, message_url
from brief.contracts import AgentReport, Priority, Signal
from brief.llm import complete
from brief.rules.prefilter import Verdict, classify_email

log = logging.getLogger(__name__)

_PRIORITY_MAP = {
    "critical": Priority.CRITICAL,
    "high": Priority.HIGH,
    "context": Priority.CONTEXT,
}

SYSTEM = """You triage the inbox of a computer science undergraduate in India.

Your single job is to decide what deserves a mention in their 7am brief, and to \
say it in one plain sentence each.

What matters to them:
- Engineering hackathons (Smart India Hackathon/SIH, Adobe, Devpost, Unstop, MLH)
- Internships, placements, interviews, and anything about an application they filed
- University business: assignments, exams, results, schedule changes, fee deadlines
- Data analytics and big data workshops, certifications, scholarships
- Anything from a real human who is waiting on a reply

What does not matter:
- Newsletters, digests, promotions, social notifications, product marketing
- Automated receipts and "your account was accessed" notices with no action
- Anything they would delete without opening

Rules:
- Assign "drop" freely. A brief with four real items beats one with twenty.
- headline: one sentence, under 110 characters, plain prose, no markup, no subject-line
  quoting. Write what happened, not what the email is called.
  Good: "Unstop says SIH internal round registration closes Friday."
  Bad:  "Email: 'SIH 2026 Registration - Act Now!'"
- action_needed: only when there is a concrete thing to do, phrased as an imperative.
- "critical" means it needs action today or a deadline is imminent. Be sparing.
- Never invent a deadline that is not stated in the email.
- Return exactly one entry per email id you were given."""


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


class TriagedEmail(BaseModel):
    ref: str = Field(description="The [id] shown in brackets for this email.")
    priority: Literal["critical", "high", "context", "drop"]
    headline: str
    detail: str | None = None
    action_needed: str | None = None


class EmailTriage(BaseModel):
    items: list[TriagedEmail]


class EmailAgent:
    name = "email"

    async def run(self, settings: Settings) -> AgentReport:
        emails = await asyncio.to_thread(fetch_recent, settings)
        report = AgentReport(agent=self.name, scanned=len(emails))
        if not emails:
            return report

        candidates: list[EmailItem] = []
        boosted: set[str] = set()
        for item in emails:
            verdict, reason = classify_email(item)
            log.debug("prefilter %s -> %s (%s)", item.id, verdict.value, reason)
            if verdict is Verdict.DROP:
                continue
            if verdict is Verdict.BOOST:
                boosted.add(item.id)
            candidates.append(item)

        log.info(
            "email: %d fetched, %d survived rules (%d boosted)",
            len(emails), len(candidates), len(boosted),
        )
        if not candidates:
            return report

        by_id = {item.id: item for item in candidates}
        try:
            triage = await self._triage(settings, candidates, boosted)
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. Catching only LLMUnavailable meant a provider
            # error code I had not enumerated (504 DEADLINE_EXCEEDED, in
            # production) escaped, failed the whole agent, and threw away eight
            # already-fetched emails. The mail is in hand by this point; no
            # model failure justifies discarding it.
            log.warning("email triage failed, falling back to rules: %s", exc)
            report.errors.append(f"LLM triage skipped: {_short(exc)}")
            report.signals = self._rule_only_signals(candidates, boosted)
            return report

        for entry in triage.items:
            if entry.priority == "drop":
                continue
            item = by_id.get(entry.ref)
            if item is None:
                # Model returned an id we never sent. Drop rather than trust it.
                log.warning("triage returned unknown ref %r", entry.ref)
                continue
            report.signals.append(
                Signal(
                    source="email",
                    priority=_PRIORITY_MAP[entry.priority],
                    headline=entry.headline[:120],
                    detail=(entry.detail or None),
                    action_needed=entry.action_needed,
                    ref=item.id,
                )
            )
        return report

    async def _triage(
        self, settings: Settings, candidates: list[EmailItem], boosted: set[str]
    ) -> EmailTriage:
        lines = []
        for item in candidates:
            marker = " <-- flagged important by rules" if item.id in boosted else ""
            lines.append(item.as_triage_line() + marker)

        prompt = (
            f"Triage these {len(candidates)} emails from the last "
            f"{settings.lookback_hours} hours.\n\n" + "\n\n".join(lines)
        )
        return await complete(
            settings=settings,
            system=SYSTEM,
            prompt=prompt,
            schema=EmailTriage,
            tier="triage",
        )

    def _rule_only_signals(
        self, candidates: list[EmailItem], boosted: set[str]
    ) -> list[Signal]:
        """Fallback when no LLM is available.

        Only boosted mail is surfaced: without a model to judge the ambiguous
        middle, showing everything would just relocate the triage work back onto
        the person the system exists to help.
        """
        return [
            Signal(
                source="email",
                priority=Priority.HIGH,
                headline=f"{item.sender_name or item.sender_email}: {item.subject}"[:120],
                detail=item.snippet[:400] or None,
                ref=item.id,
            )
            for item in candidates
            if item.id in boosted
        ]


__all__ = ["EmailAgent", "message_url"]
