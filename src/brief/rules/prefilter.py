"""Deterministic filtering. Your taste, expressed as data.

This runs before any LLM sees anything, and it is the cheapest filter in the
system: zero tokens, zero latency, zero cost. Its job is to remove what is
obviously noise and to force-promote what is obviously important, so the LLM
only spends judgment on the genuinely ambiguous middle.

This is the file you edit as the brief learns your life. Everything tunable
lives in the block below; the logic underneath rarely needs to change.
"""

from __future__ import annotations

import re
from enum import Enum

from brief.connectors.gmail import EmailItem

# ---------------------------------------------------------------------------
# PERSONAL RULES - edit freely
# ---------------------------------------------------------------------------

# Senders whose mail is never worth a morning mention. Matched against the
# sender's domain, so "quora.com" also covers "mail.quora.com".
NOISE_DOMAINS: set[str] = {
    "quora.com",
    "facebookmail.com",
    "facebook.com",
    "instagram.com",
    "pinterest.com",
    "meetup.com",
    "medium.com",
    "substack.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "reddit.com",
}

# Matched case-insensitively anywhere in the sender name or address.
NOISE_SENDER_PATTERNS: tuple[str, ...] = (
    "no-reply",
    "noreply",
    "newsletter",
    "digest",
    "notification",
    "marketing",
    "promo",
)

# Subject/snippet phrases that mark mail as pure marketing even from an
# otherwise legitimate sender.
NOISE_SUBJECT_PATTERNS: tuple[str, ...] = (
    "unsubscribe",
    "% off",
    "flash sale",
    "limited time offer",
    "upgrade your plan",
    "webinar replay",
)

# The things you actually want woken up for. A hit here overrides every drop
# rule above - a hackathon invite from a "noreply@" address still gets through.
CRITICAL_KEYWORDS: tuple[str, ...] = (
    "smart india hackathon",
    "sih",
    "hackathon",
    "internship",
    "interview",
    "shortlisted",
    "selected",
    "offer letter",
    "deadline",
    "last date",
    "exam",
    "assignment",
    "viva",
    "practical",
    "result",
    "admit card",
)

HIGH_KEYWORDS: tuple[str, ...] = (
    "workshop",
    "adobe",
    "devpost",
    "unstop",
    "hackerrank",
    "leetcode contest",
    "data analytics",
    "big data",
    "scholarship",
    "certification",
    "placement",
    "resume",
    "referral",
)

# Domains that always matter regardless of keywords: your university, and the
# platforms that carry opportunities. Add your college domain here.
ALWAYS_KEEP_DOMAINS: set[str] = {
    "unstop.com",
    "devpost.com",
    "dora.hackerearth.com",
    "hackerearth.com",
    "mlh.io",
    "github.com",
}

# Calendar titles matching these are CRITICAL regardless of how far out they are.
CALENDAR_CRITICAL_PATTERNS: tuple[str, ...] = (
    "exam",
    "assignment",
    "submission",
    "deadline",
    "viva",
    "practical",
    "presentation",
    "interview",
    "test",
    "quiz",
)

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    DROP = "drop"      # never reaches the LLM
    BOOST = "boost"    # always reaches the LLM, pre-marked as important
    PASS = "pass"      # ambiguous - let the LLM judge


def _contains_any(haystack: str, needles: tuple[str, ...]) -> str | None:
    for needle in needles:
        # Word-boundary match so "sih" doesn't fire on "basic" or "inside".
        if re.search(rf"\b{re.escape(needle)}\b", haystack):
            return needle
    return None


def classify_email(item: EmailItem) -> tuple[Verdict, str]:
    """Return a verdict and a short human-readable reason.

    The reason string exists so `--dry-run --explain` can show you exactly why
    something was dropped. Silent filtering is how a brief quietly starts
    missing things.
    """
    haystack = f"{item.subject} {item.snippet}".lower()
    sender_blob = f"{item.sender_name} {item.sender_email}".lower()

    # Boosts win over every drop rule below.
    if hit := _contains_any(haystack, CRITICAL_KEYWORDS):
        return Verdict.BOOST, f"critical keyword: {hit}"
    if item.sender_domain in ALWAYS_KEEP_DOMAINS:
        return Verdict.BOOST, f"always-keep domain: {item.sender_domain}"
    if hit := _contains_any(haystack, HIGH_KEYWORDS):
        return Verdict.BOOST, f"priority keyword: {hit}"

    if item.sender_domain in NOISE_DOMAINS:
        return Verdict.DROP, f"noise domain: {item.sender_domain}"
    if hit := next((p for p in NOISE_SENDER_PATTERNS if p in sender_blob), None):
        return Verdict.DROP, f"noise sender pattern: {hit}"
    if hit := next((p for p in NOISE_SUBJECT_PATTERNS if p in haystack), None):
        return Verdict.DROP, f"marketing phrase: {hit}"

    # Bulk mail that survived the checks above is still probably a list.
    # It is dropped last so a genuine keyword hit above can rescue it.
    if item.has_unsubscribe:
        return Verdict.DROP, "bulk mail (List-Unsubscribe header)"

    return Verdict.PASS, "no rule matched"


def is_critical_event(summary: str) -> bool:
    return _contains_any(summary.lower(), CALENDAR_CRITICAL_PATTERNS) is not None


__all__ = ["Verdict", "classify_email", "is_critical_event"]
