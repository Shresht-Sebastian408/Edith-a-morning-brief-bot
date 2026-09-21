"""Rules are where personal taste lives, so this is where regressions happen.

These tests are the guard rail for editing prefilter.py: they assert that the
things you care about survive and the things you don't, don't.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brief.connectors.gmail import EmailItem
from brief.rules.prefilter import Verdict, classify_email, is_critical_event


def email(**kw) -> EmailItem:
    base = dict(
        id="m1",
        sender_name="Someone",
        sender_email="someone@example.com",
        subject="Hello",
        snippet="Just checking in",
        received=datetime.now(tz=timezone.utc),
        has_unsubscribe=False,
        labels=["INBOX"],
    )
    return EmailItem(**{**base, **kw})


@pytest.mark.parametrize(
    "item",
    [
        email(sender_email="digest@quora.com", subject="10 answers for you"),
        email(sender_email="noreply@facebookmail.com", subject="You have notifications"),
        email(sender_email="hello@shop.com", subject="50% off everything today"),
        email(sender_email="news@somebrand.com", subject="Our weekly roundup",
              has_unsubscribe=True),
        email(sender_email="marketing@tool.io", subject="Upgrade your plan"),
    ],
    ids=["quora", "facebook", "sale", "bulk-list", "marketing-sender"],
)
def test_noise_is_dropped(item):
    verdict, _ = classify_email(item)
    assert verdict is Verdict.DROP


@pytest.mark.parametrize(
    "item",
    [
        email(subject="SIH 2026 internal hackathon registration open"),
        email(subject="Your internship application has been shortlisted"),
        email(subject="Assignment 3 submission deadline extended"),
        email(sender_email="team@unstop.com", subject="Weekly opportunities"),
        email(subject="Big data analytics workshop this Saturday"),
        email(subject="Adobe University Hackathon - final round"),
    ],
    ids=["sih", "internship", "assignment", "unstop", "workshop", "adobe"],
)
def test_opportunities_survive(item):
    verdict, _ = classify_email(item)
    assert verdict is Verdict.BOOST


def test_priority_beats_noise_sender():
    """A hackathon invite from a noreply address must still get through.

    This is the rule ordering that matters most: nearly every genuine
    opportunity email is sent from an automated address.
    """
    verdict, reason = classify_email(
        email(
            sender_email="noreply@devpost.com",
            subject="Your hackathon submission is due tomorrow",
            has_unsubscribe=True,
        )
    )
    assert verdict is Verdict.BOOST, reason


def test_ambiguous_mail_reaches_the_llm():
    verdict, _ = classify_email(
        email(sender_email="kartikeya@gmail.com", subject="can you send me the notes")
    )
    assert verdict is Verdict.PASS


def test_keywords_match_on_word_boundaries():
    """'sih' must not fire inside 'inside', 'basic', etc."""
    verdict, _ = classify_email(email(subject="Inside our basic design process"))
    assert verdict is not Verdict.BOOST


@pytest.mark.parametrize(
    "title,expected",
    [
        ("DBMS Exam", True),
        ("OS Assignment submission", True),
        ("Data Structures Lecture", False),
        ("Lunch with Devansh", False),
        ("Mock Interview - placement cell", True),
    ],
)
def test_calendar_criticality(title, expected):
    assert is_critical_event(title) is expected
