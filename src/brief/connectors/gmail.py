"""Gmail connector over IMAP, authenticated with an app password.

Why IMAP rather than the Gmail API: publishing an OAuth app to production now
requires a homepage, a privacy policy and a DNS-verified domain you own, which
is disproportionate for a tool with exactly one user. An app password needs
none of that and does not expire.

Two things make this as good as the API for our purposes:

  X-GM-RAW   lets IMAP SEARCH take full Gmail query syntax, so category
             filtering still happens server-side and costs nothing.
  BODY.PEEK  fetches without setting the Seen flag, so reading your inbox
             never marks anything as read.

Returns the same `EmailItem` the API version did, so nothing downstream changed.
"""

from __future__ import annotations

import email
import html
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from brief.config import IMAP_HOST, IMAP_PORT, Settings

log = logging.getLogger(__name__)

# Gmail's own query language, evaluated server-side. Anything excluded here
# never crosses the network, let alone becomes a token.
BASE_QUERY = "-category:promotions -category:social -in:chats"

_FETCH_SPEC = "(UID X-GM-MSGID X-GM-LABELS BODY.PEEK[]<0.16384>)"
_BATCH = 25

_RE_MSGID = re.compile(rb"X-GM-MSGID\s+(\d+)")
_RE_LABELS = re.compile(rb"X-GM-LABELS\s+\(([^)]*)\)")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_LABEL_TOKEN = re.compile(r'"[^"]*"|\S+')
_RE_WHITESPACE = re.compile(r"\s+")


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

        Terse on purpose: this string is multiplied by the number of surviving
        emails, so every word is paid for once per message.
        """
        when = self.received.strftime("%a %H:%M") if self.received else "unknown"
        return (
            f"[{self.id}] from={self.sender_name or self.sender_email} "
            f"<{self.sender_email}> at={when}\n"
            f"  subject: {self.subject}\n"
            f"  preview: {self.snippet[:220]}"
        )


def _decode(raw: str | None) -> str:
    """Decode RFC 2047 encoded headers (=?utf-8?B?...?=) into plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw.strip()


def _preview(msg: Message, limit: int = 400) -> str:
    """Best-effort plain-text preview, mirroring the API's `snippet`."""
    text = ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            decoded = _RE_TAG.sub(" ", decoded)
        text = html.unescape(decoded)
        if ctype == "text/plain":
            break  # prefer plain text when both alternatives are present
    return _RE_WHITESPACE.sub(" ", text).strip()[:limit]


def _parse_prefix(prefix: bytes) -> tuple[str, list[str]]:
    """Pull X-GM-MSGID and X-GM-LABELS out of the FETCH response line."""
    msgid_match = _RE_MSGID.search(prefix)
    msgid = msgid_match.group(1).decode() if msgid_match else ""

    labels: list[str] = []
    labels_match = _RE_LABELS.search(prefix)
    if labels_match:
        raw = labels_match.group(1).decode("utf-8", errors="replace")
        # Labels are space-separated, quoted when they contain spaces, and
        # system labels are backslash-prefixed (\\Inbox, \\Important).
        labels = [
            token.strip('"').lstrip("\\")
            for token in _RE_LABEL_TOKEN.findall(raw)
        ]
    return msgid, labels


def _to_item(prefix: bytes, raw: bytes) -> EmailItem | None:
    msgid, labels = _parse_prefix(prefix)
    if not msgid:
        return None

    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_decode(msg.get("From")))

    received: datetime | None = None
    date_header = msg.get("Date")
    if date_header:
        try:
            received = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            received = None

    return EmailItem(
        id=msgid,
        sender_name=name.strip(),
        sender_email=addr.lower().strip(),
        subject=_decode(msg.get("Subject")),
        snippet=_preview(msg),
        received=received,
        has_unsubscribe=bool(msg.get("List-Unsubscribe")),
        labels=labels,
    )


def fetch_recent(
    settings: Settings,
    *,
    lookback_hours: int | None = None,
    max_results: int = 80,
) -> list[EmailItem]:
    """Fetch messages received in the lookback window."""
    settings.require("gmail_address", "gmail_app_password")
    hours = lookback_hours if lookback_hours is not None else settings.lookback_hours
    after = int((datetime.now(tz=timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"after:{after} {BASE_QUERY}"

    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(settings.gmail_address, settings.gmail_app_password)
        # readonly=True is belt-and-braces alongside BODY.PEEK.
        conn.select("INBOX", readonly=True)

        typ, data = conn.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ} {data!r}")

        uids = (data[0] or b"").split()
        if not uids:
            return []
        uids = uids[-max_results:]  # newest last; cap the oldest away
        log.info("imap: %d messages match", len(uids))

        items: list[EmailItem] = []
        for start in range(0, len(uids), _BATCH):
            chunk = b",".join(uids[start : start + _BATCH])
            typ, response = conn.uid("FETCH", chunk, _FETCH_SPEC)
            if typ != "OK":
                log.warning("IMAP fetch failed for a batch: %s", typ)
                continue
            for part in response:
                if isinstance(part, tuple) and len(part) == 2:
                    item = _to_item(part[0], part[1])
                    if item is not None:
                        items.append(item)
        return items
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - a cleanup error must not mask the real one
            pass


def message_url(message_id: str) -> str:
    """Deep link into Gmail. X-GM-MSGID is decimal; the web UI wants hex."""
    try:
        return f"https://mail.google.com/mail/u/0/#all/{int(message_id):x}"
    except (TypeError, ValueError):
        return "https://mail.google.com/mail/u/0/#inbox"


__all__ = ["EmailItem", "fetch_recent", "message_url", "BASE_QUERY"]
