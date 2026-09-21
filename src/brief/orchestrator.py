"""The supervisor.

Fans out to agents concurrently, then synthesises their reports into one brief.

The important property: this module imports `Signal`, not `EmailItem` or
`CalendarItem`. It has no idea what Gmail's API returns and cannot accidentally
grow a dependency on it. Adding a WhatsApp agent later means appending one line
to AGENTS - the synthesis prompt below does not change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pydantic import BaseModel, Field

from brief.agents.base import Agent, run_safely
from brief.agents.calendar_agent import CalendarAgent
from brief.agents.email_agent import EmailAgent
from brief.config import Settings
from brief.contracts import AgentReport, Brief, BriefSection, Priority, Signal
from brief.llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

AGENTS: list[Agent] = [EmailAgent(), CalendarAgent()]

SYSTEM = """You write a single morning brief for a computer science undergraduate \
in India, from signals already triaged by specialist agents.

You are given structured signals, not raw data. Trust their priorities. Your job \
is composition, not re-triage.

Produce two things:

1. voice_script - spoken aloud while they get ready, so it must work with no screen.
   - Flowing prose. No bullet points, no markdown, no URLs, no emoji, no headings.
   - 120 to 180 words. Shorter on a quiet day; never pad.
   - Open with the day and what shapes it, then the urgent items, then the rest.
   - Say times naturally: "quarter past nine", not "09:15".
   - Speak only CRITICAL and HIGH signals. Compress CONTEXT into one clause if at
     all, e.g. "plus a few routine classes."
   - Write for the ear: short sentences, no nested clauses, no parentheses.
   - Address them directly as "you". Warm and brisk, not chirpy. No "Good morning!"
     exclamations, no motivational sign-off.

2. text_sections - the written brief, for reference later in the day.
   - Group by theme, not by which agent produced it. "Today's schedule",
     "Needs a reply", "Opportunities", "Deadlines" are better than "Email" and
     "Calendar".
   - Each bullet one line, scannable, front-loaded with the thing that matters.
   - Omit a section entirely rather than emitting it empty.

If there is genuinely nothing important, say so plainly and briefly. A ten-second
"nothing needs you today, here are your three classes" is a good brief."""


class BriefDraft(BaseModel):
    greeting: str = Field(description="One short line, e.g. 'Monday, 22 September.'")
    voice_script: str
    text_sections: list[BriefSection] = Field(default_factory=list)


async def gather_reports(settings: Settings) -> list[AgentReport]:
    """Run every agent concurrently."""
    reports = await asyncio.gather(*(run_safely(a, settings) for a in AGENTS))
    for report in reports:
        log.info(
            "agent=%s scanned=%d signals=%d errors=%d",
            report.agent, report.scanned, len(report.signals), len(report.errors),
        )
    return list(reports)


def collect_signals(reports: list[AgentReport]) -> list[Signal]:
    signals = [s for r in reports for s in r.signals]
    signals.sort(key=lambda s: s.sort_key())
    return signals


def _render_signals(signals: list[Signal], now: datetime) -> str:
    lines = [f"Today is {now:%A, %d %B %Y}. Current time {now:%H:%M} IST.", ""]
    for signal in signals:
        parts = [f"- [{signal.priority.name}] ({signal.source}) {signal.headline}"]
        if signal.detail:
            parts.append(f"    detail: {signal.detail}")
        if signal.action_needed:
            parts.append(f"    action: {signal.action_needed}")
        if signal.deadline:
            parts.append(f"    when: {signal.deadline:%a %d %b %H:%M}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


async def build_brief(settings: Settings, reports: list[AgentReport]) -> Brief:
    signals = collect_signals(reports)
    now = datetime.now(tz=settings.tz)
    loud = [s for s in signals if s.priority >= Priority.HIGH]

    failed = [r for r in reports if r.errors]

    if not signals:
        return _empty_brief(now, failed)

    try:
        draft = await complete(
            settings=settings,
            system=SYSTEM,
            prompt=_render_signals(signals, now),
            schema=BriefDraft,
            tier="synthesis",
        )
    except LLMUnavailable as exc:
        log.warning("synthesis unavailable, assembling brief mechanically: %s", exc)
        return _mechanical_brief(now, signals, loud)

    brief = Brief(
        greeting=draft.greeting,
        voice_script=draft.voice_script,
        text_sections=draft.text_sections,
        headline_count=len(loud),
    )
    _append_failures(brief, failed)
    return brief


def _empty_brief(now: datetime, failed: list[AgentReport]) -> Brief:
    """No signals. Distinguish a genuinely quiet day from a broken pipeline.

    Reporting "your inbox is clear" when every connector is down is worse than
    reporting nothing at all: it is a confident claim that happens to be false,
    and you would act on it.
    """
    if failed and len(failed) == len(AGENTS):
        names = " and ".join(r.agent for r in failed)
        brief = Brief(
            greeting=f"{now:%A, %d %B} - brief unavailable.",
            voice_script=(
                f"Good morning. I couldn't reach {names} this morning, so I have "
                "nothing to tell you. This is a fault on my side, not a quiet day. "
                "Check the run log when you get a chance."
            ),
            text_sections=[],
            headline_count=0,
        )
        _append_failures(brief, failed)
        return brief

    brief = Brief(
        greeting=f"{now:%A, %d %B}.",
        voice_script=(
            f"Good morning. It's {now:%A}. Nothing needs your attention right now, "
            "your inbox is clear and there's nothing on the calendar. Enjoy the quiet one."
        ),
        text_sections=[],
        headline_count=0,
    )
    _append_failures(brief, failed)
    return brief


def _append_failures(brief: Brief, failed: list[AgentReport]) -> None:
    """Surface partial failures in the text brief.

    Deliberately not in the voice script - you can't act on a stack trace while
    brushing your teeth, but you should still know the brief was incomplete.
    """
    if not failed:
        return
    brief.text_sections.append(
        BriefSection(
            title="Incomplete - some sources failed",
            bullets=[f"{r.agent}: {e}" for r in failed for e in r.errors],
        )
    )


def _mechanical_brief(
    now: datetime, signals: list[Signal], loud: list[Signal]
) -> Brief:
    """Last-resort brief with no model involved.

    Deliberately plain. This exists so a provider outage degrades the brief's
    prose rather than cancelling the morning entirely.
    """
    spoken = " ".join(f"{s.headline}." for s in loud[:6]) or "Nothing urgent today."
    sections: list[BriefSection] = []
    for priority in (Priority.CRITICAL, Priority.HIGH, Priority.CONTEXT):
        bullets = [s.headline for s in signals if s.priority is priority]
        if bullets:
            sections.append(BriefSection(title=priority.name.title(), bullets=bullets))
    return Brief(
        greeting=f"{now:%A, %d %B}.",
        voice_script=f"Good morning. {spoken}",
        text_sections=sections,
        headline_count=len(loud),
    )
