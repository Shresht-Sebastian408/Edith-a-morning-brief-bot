"""The data contract between agents and the orchestrator.

This module is the architectural spine of the system. The rule it enforces:

    Raw source data never reaches the orchestrator.

Each agent owns one noisy source (an inbox, a calendar) and is responsible for
reducing it to a short list of `Signal` objects. The orchestrator only ever sees
`Signal`s. A 60-email inbox arrives as ~6 signals of ~30 tokens each.

That is what keeps the supervisor's context small and its cost flat as agents are
added: a future WhatsApp agent costs the orchestrator nothing extra, because it
speaks this same contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["email", "calendar", "whatsapp", "instagram"]


class Priority(IntEnum):
    """How loudly a signal should appear in the brief.

    Ordered so that `sorted(signals, reverse=True)` puts the urgent things first.
    """

    CRITICAL = 3  # Needs action today. Named aloud in the voice brief.
    HIGH = 2      # Worth knowing this morning. Named aloud.
    CONTEXT = 1   # Background. Text brief only, usually summarised in aggregate.


class Signal(BaseModel):
    """One thing worth knowing. The atomic unit crossing the agent boundary."""

    source: SourceName
    priority: Priority
    headline: str = Field(
        max_length=120,
        description="One line, plain prose, no markup. Read aloud verbatim.",
    )
    detail: str | None = Field(
        default=None,
        max_length=400,
        description="Supporting context for the text brief. Never spoken.",
    )
    action_needed: str | None = Field(
        default=None,
        description="The concrete next step, if any. e.g. 'Register by Friday'.",
    )
    deadline: datetime | None = None
    ref: str | None = Field(
        default=None,
        description="Source-native id (Gmail message id, Calendar event id) for deep links.",
    )

    def sort_key(self) -> tuple[int, float]:
        """Most urgent first; within a priority, soonest deadline first."""
        deadline_rank = self.deadline.timestamp() if self.deadline else float("inf")
        return (-int(self.priority), deadline_rank)


class AgentReport(BaseModel):
    """What one agent hands back to the orchestrator.

    Agents must not raise into the orchestrator. A failed Gmail token produces a
    report with `errors` populated and no signals, and the morning brief still
    goes out with whatever else succeeded. For a tool you rely on daily, a partial
    brief beats a missing one.
    """

    agent: str
    signals: list[Signal] = Field(default_factory=list)
    scanned: int = Field(default=0, description="Raw items considered before filtering.")
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @classmethod
    def failed(cls, agent: str, error: str) -> AgentReport:
        return cls(agent=agent, errors=[error])


class Brief(BaseModel):
    """The orchestrator's synthesis. The only thing the delivery layer sees."""

    greeting: str = Field(description="One short opening line, e.g. 'Sunday, 21 September.'")
    voice_script: str = Field(
        description=(
            "What gets spoken aloud. Flowing prose, roughly 120-180 words, no URLs, "
            "no markdown, no bullet characters, no emoji. Read while getting ready."
        )
    )
    text_sections: list["BriefSection"] = Field(
        default_factory=list,
        description="The written brief, grouped by theme rather than by source agent.",
    )
    headline_count: int = 0

    def is_quiet_day(self) -> bool:
        return self.headline_count == 0


class BriefSection(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)


Brief.model_rebuild()
