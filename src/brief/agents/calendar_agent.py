"""Schedule & academic agent.

Deliberately LLM-free. Calendar events arrive already structured, already
titled, and already timed - there is no ambiguity for a model to resolve, so
spending a call here would buy nothing. Priority comes from two signals a rule
can read perfectly well: whether the title looks academic-critical, and how soon
it starts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from brief.config import Settings
from brief.connectors.gcal import CalendarItem, fetch_upcoming
from brief.contracts import AgentReport, Priority, Signal
from brief.rules.prefilter import is_critical_event


class CalendarAgent:
    name = "calendar"

    async def run(self, settings: Settings) -> AgentReport:
        events = await asyncio.to_thread(fetch_upcoming, settings)
        now = datetime.now(tz=settings.tz)

        signals = [self._to_signal(event, now) for event in events]
        return AgentReport(agent=self.name, signals=signals, scanned=len(events))

    def _to_signal(self, event: CalendarItem, now: datetime) -> Signal:
        hours_away = (
            (event.start - now).total_seconds() / 3600 if event.start else 999.0
        )

        if is_critical_event(event.summary):
            priority = Priority.CRITICAL
        elif hours_away <= 14:
            # Happens today, while the brief is still relevant.
            priority = Priority.HIGH
        else:
            priority = Priority.CONTEXT

        if event.all_day:
            when = "all day"
        elif event.start:
            when = f"{event.start:%H:%M}"
            if hours_away > 14:
                when = f"{event.start:%a} {when}"
        else:
            when = "unscheduled"

        headline = f"{when} - {event.summary}"
        if event.location:
            headline += f" ({event.location})"

        return Signal(
            source="calendar",
            priority=priority,
            headline=headline[:120],
            detail=event.description[:400] or None,
            deadline=event.start,
            ref=event.id,
        )
