"""Orchestration and rendering, with no network and no model involved."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brief.contracts import AgentReport, Brief, BriefSection, Priority, Signal
from brief.delivery.telegram import _chunk, render_html
from brief.orchestrator import _mechanical_brief, collect_signals


def sig(priority: Priority, headline: str, deadline=None, source="email") -> Signal:
    return Signal(source=source, priority=priority, headline=headline, deadline=deadline)


def test_signals_sort_urgent_first():
    reports = [
        AgentReport(agent="a", signals=[sig(Priority.CONTEXT, "routine class")]),
        AgentReport(agent="b", signals=[sig(Priority.CRITICAL, "exam today")]),
        AgentReport(agent="c", signals=[sig(Priority.HIGH, "hackathon closes")]),
    ]
    assert [s.headline for s in collect_signals(reports)] == [
        "exam today",
        "hackathon closes",
        "routine class",
    ]


def test_equal_priority_sorts_by_soonest_deadline():
    now = datetime.now(tz=timezone.utc)
    reports = [
        AgentReport(
            agent="a",
            signals=[
                sig(Priority.HIGH, "later", deadline=now + timedelta(hours=8)),
                sig(Priority.HIGH, "sooner", deadline=now + timedelta(hours=2)),
                sig(Priority.HIGH, "undated"),
            ],
        )
    ]
    assert [s.headline for s in collect_signals(reports)] == ["sooner", "later", "undated"]


def test_mechanical_brief_survives_without_a_model():
    """A provider outage must degrade the prose, not cancel the morning."""
    now = datetime.now(tz=timezone.utc)
    signals = [sig(Priority.CRITICAL, "DBMS exam at 10am"), sig(Priority.CONTEXT, "gym")]
    loud = [s for s in signals if s.priority >= Priority.HIGH]

    brief = _mechanical_brief(now, signals, loud)

    assert "DBMS exam at 10am" in brief.voice_script
    assert brief.headline_count == 1
    assert {s.title for s in brief.text_sections} == {"Critical", "Context"}


def test_render_escapes_html_from_email_subjects():
    """Subjects are attacker-adjacent text; unescaped '<' breaks the send."""
    brief = Brief(
        greeting="Monday, 22 September.",
        voice_script="...",
        text_sections=[BriefSection(title="Needs a reply", bullets=["<script>x</script> & co"])],
        headline_count=1,
    )
    out = render_html(brief)
    assert "&lt;script&gt;" in out
    assert "&amp; co" in out
    assert "<script>" not in out


def test_long_brief_splits_on_line_boundaries():
    text = "\n".join(f"bullet number {i}" for i in range(600))
    chunks = _chunk(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    # Nothing may be lost or duplicated in the split.
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_quiet_day_is_representable():
    brief = Brief(greeting="Sunday.", voice_script="Nothing today.", headline_count=0)
    assert brief.is_quiet_day()
    assert "Nothing needs you" in render_html(brief)


def test_total_failure_is_not_reported_as_a_quiet_day():
    """A broken pipeline must never look like good news.

    "Your inbox is clear" when every connector is down is a confident false
    claim, and one you would act on.
    """
    from brief.orchestrator import AGENTS, _empty_brief

    now = datetime.now(tz=timezone.utc)
    failed = [AgentReport.failed(a.name, "token expired") for a in AGENTS]

    brief = _empty_brief(now, failed)

    assert "incomplete" in brief.greeting
    assert "couldn't reach" in brief.voice_script
    assert "inbox is clear" not in brief.voice_script
    # The failure detail belongs in text, not read aloud.
    assert "token expired" not in brief.voice_script
    assert any("token expired" in b for s in brief.text_sections for b in s.bullets)


def test_genuinely_quiet_day_still_reads_as_quiet():
    from brief.orchestrator import _empty_brief

    brief = _empty_brief(datetime.now(tz=timezone.utc), [])

    assert "inbox is clear" in brief.voice_script
    assert brief.text_sections == []
    assert "Nothing needs you" in render_html(brief)


def test_partial_failure_is_flagged_without_hijacking_the_voice_script():
    from brief.orchestrator import _append_failures

    brief = Brief(greeting="Monday.", voice_script="Two classes today.", headline_count=1)
    _append_failures(brief, [AgentReport.failed("email", "rate limited")])

    assert brief.voice_script == "Two classes today."
    assert brief.text_sections[-1].title.startswith("Incomplete")


def test_a_partial_failure_is_not_a_quiet_day_either():
    """One dead connector and no signals is not "your inbox is clear".

    Regression test: a 504 from the model provider killed the email agent while
    the calendar agent succeeded with nothing scheduled. The brief then claimed
    the inbox was clear, having never successfully read it.
    """
    from brief.orchestrator import _empty_brief

    brief = _empty_brief(
        datetime.now(tz=timezone.utc),
        [AgentReport.failed("email", "504 DEADLINE_EXCEEDED")],
    )

    assert "inbox is clear" not in brief.voice_script
    assert "couldn't reach email" in brief.voice_script
