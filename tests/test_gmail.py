"""IMAP response parsing, against synthetic raw messages.

Email parsing is where silent corruption hides: an encoded subject that renders
as mojibake, an HTML-only newsletter with an empty preview, a multipart message
whose text is buried. Each of those degrades triage quality without ever
raising, so they are pinned here.
"""

from __future__ import annotations

from brief.connectors.gmail import _decode, _parse_prefix, _preview, _to_item, message_url


def raw(headers: str, body: str = "Hello there.") -> bytes:
    return f"{headers}\r\n\r\n{body}".encode()


PREFIX = b"1 (UID 42 X-GM-MSGID 1834567890123456789 X-GM-LABELS (\\Inbox \\Important) BODY[]<0> {100}"


def test_parses_a_plain_message():
    item = _to_item(
        PREFIX,
        raw(
            "From: Kartikeya <kartikeya@gmail.com>\r\n"
            "Subject: Notes for tomorrow\r\n"
            "Date: Mon, 21 Sep 2026 09:15:00 +0530"
        ),
    )

    assert item is not None
    assert item.id == "1834567890123456789"
    assert item.sender_name == "Kartikeya"
    assert item.sender_email == "kartikeya@gmail.com"
    assert item.sender_domain == "gmail.com"
    assert item.subject == "Notes for tomorrow"
    assert item.has_unsubscribe is False
    assert item.received is not None and item.received.hour == 9


def test_decodes_rfc2047_encoded_subjects():
    """Encoded subjects must not reach the model as raw =?utf-8?B?...?= noise."""
    assert _decode("=?utf-8?B?SGFja2F0aG9uIOKAkyBmaW5hbCByb3VuZA==?=") == (
        "Hackathon – final round"
    )


def test_detects_list_unsubscribe():
    item = _to_item(
        PREFIX,
        raw(
            "From: news@brand.com\r\n"
            "Subject: Weekly roundup\r\n"
            "List-Unsubscribe: <https://brand.com/unsub>"
        ),
    )
    assert item is not None and item.has_unsubscribe is True


def test_system_labels_are_stripped_of_backslashes():
    _, labels = _parse_prefix(PREFIX)
    assert labels == ["Inbox", "Important"]


def test_quoted_labels_with_spaces_survive():
    _, labels = _parse_prefix(
        b'1 (X-GM-LABELS ("Category Promotions" \\Inbox) BODY[]<0> {10}'
    )
    assert labels == ["Category Promotions", "Inbox"]


def test_html_only_mail_still_yields_a_preview():
    """Marketing mail is often HTML-only; an empty preview blinds triage."""
    msg_bytes = raw(
        "From: a@b.com\r\nSubject: S\r\n"
        'Content-Type: text/html; charset="utf-8"',
        "<html><body><p>Registration closes <b>Friday</b>.</p></body></html>",
    )
    import email

    preview = _preview(email.message_from_bytes(msg_bytes))
    assert "Registration closes" in preview
    assert "<" not in preview


def test_multipart_prefers_the_plain_text_alternative():
    msg_bytes = (
        b"From: a@b.com\r\n"
        b"Subject: S\r\n"
        b'Content-Type: multipart/alternative; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"The plain version.\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>The HTML version.</p>\r\n"
        b"--BOUND--\r\n"
    )
    import email

    assert "plain version" in _preview(email.message_from_bytes(msg_bytes))


def test_preview_collapses_whitespace():
    import email

    msg_bytes = raw("From: a@b.com\r\nSubject: S", "line one\r\n\r\n\r\n   line two")
    assert _preview(email.message_from_bytes(msg_bytes)) == "line one line two"


def test_a_response_without_a_msgid_is_discarded():
    """Never fabricate an id - a wrong one produces a broken deep link."""
    assert _to_item(b"1 (UID 42 BODY[]<0> {10}", raw("From: a@b.com\r\nSubject: S")) is None


def test_message_url_converts_decimal_msgid_to_hex():
    assert message_url("1834567890123456789").endswith(hex(1834567890123456789)[2:])
    # A malformed id must degrade to a usable link, not crash the brief.
    assert message_url("not-a-number").startswith("https://mail.google.com")


def test_preview_discards_css_and_script_bodies():
    """Tag-stripping alone leaves stylesheet text, which floods the preview.

    Regression test: real marketing mail produced previews reading
    "@media (max-width: 600px) { .main-card { width: 100% !important" instead
    of the actual message, wasting tokens and degrading triage.
    """
    import email

    msg_bytes = raw(
        "From: a@b.com\r\nSubject: S\r\n"
        'Content-Type: text/html; charset="utf-8"',
        "<html><head><style>@media (max-width:600px){.card{width:100%!important}}</style></head>"
        "<body><!-- preheader --><p>Google is hiring interns.</p></body></html>",
    )
    preview = _preview(email.message_from_bytes(msg_bytes))

    assert preview == "Google is hiring interns."
    assert "@media" not in preview
    assert "preheader" not in preview
