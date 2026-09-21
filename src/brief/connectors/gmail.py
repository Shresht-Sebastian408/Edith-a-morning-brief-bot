"""Gmail read-only connector.

Returns lightweight `EmailItem`s, never raw Gmail payloads. Bodies are
deliberately not fetched: metadata format gives us sender, subject, snippet and
headers in one call, which is everything the triage stage needs and a fraction
of the bytes (and, later, of the tokens).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

from brief.config import Settings
from brief.connectors.google_auth import build_service

# Gmail's own query language does the first pass server-side, for free.
# Anything excluded here never becomes a Python object, let alone a token.
BASE_QUERY = "-category:promotions -category:social -in:chats"

_METADATA_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe"]


@dataclass(slots=True)
class EmailItem:
    """One email, reduced to what triage actually reasons about."""

    id: str
    sender_name: str
    sender_email: str
    subject: str
    snippet: str
    received: datetime | None
    has_unsubscribe: bool
    labels: list[str]

    @property
    def sender_domain(self) -> str:
        _, _, domain = self.sender_email.partition("@")
        return domain.lower()

    def as_triage_line(self) -> str:
        """Compact single-line rendering handed to the LLM.

        Kept terse on purpose: this string is multiplied by the number of
        surviving emails, so every word here is paid for once per message.
        """
        when = self.received.strftime("%a %H:%M") if self.received else "unknown"
        return (
            f"[{self.id}] from={self.sender_name or self.sender_email} "
            f"<{self.sender_email}> at={when}\n"
            f"  subject: {self.subject}\n"
            f"  preview: {self.snippet[:220]}"
        )


def _decode_header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def _parse_received(raw_date: str, internal_ms: str | None) -> datetime | None:
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            pass
    if internal_ms:
        try:
            return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return None


def fetch_recent(
    settings: Settings,
    *,
    lookback_hours: int | None = None,
    max_results: int = 80,
) -> list[EmailItem]:
    """Fetch metadata for messages received in the lookback window."""
    hours = lookback_hours if lookback_hours is not None else settings.lookback_hours
    after = int((datetime.now(tz=timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"after:{after} {BASE_QUERY}"

    service = build_service("gmail", "v1", settings)
    messages = service.users().messages()

    listing = messages.list(userId="me", q=query, maxResults=max_results).execute()
    refs = listing.get("messages", [])

    items: list[EmailItem] = []
    for ref in refs:
        msg = messages.get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=_METADATA_HEADERS,
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        name, addr = parseaddr(_decode_header(headers, "From"))

        items.append(
            EmailItem(
                id=msg["id"],
                sender_name=name.strip(),
                sender_email=addr.lower().strip(),
                subject=_decode_header(headers, "Subject").strip(),
                snippet=_unescape_snippet(msg.get("snippet", "")),
                received=_parse_received(
                    _decode_header(headers, "Date"), msg.get("internalDate")
                ),
                has_unsubscribe=bool(_decode_header(headers, "List-Unsubscribe")),
                labels=msg.get("labelIds", []),
            )
        )
    return items


def _unescape_snippet(snippet: str) -> str:
    import html

    return html.unescape(snippet).replace("‌", "").strip()


def message_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


__all__ = ["EmailItem", "fetch_recent", "message_url", "BASE_QUERY"]
