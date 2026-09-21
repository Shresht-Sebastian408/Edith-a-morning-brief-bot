"""Calendar parsing, against a synthetic iCal feed.

The important case is recurrence: an .ics feed ships a weekly lecture as one
VEVENT with an RRULE, not as thirty events. If expansion breaks, the brief
silently stops mentioning every recurring class.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brief.config import Settings
from brief.connectors import gcal


def ics(body: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "X-WR-CALNAME:Test Calendar\r\n"
        f"{body}"
        "END:VCALENDAR\r\n"
    ).encode()


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(calendar_ical_url="https://example.com/secret.ics")


def patch_feed(monkeypatch, content: bytes) -> None:
    monkeypatch.setattr(gcal.httpx, "get", lambda *a, **k: FakeResponse(content))


def test_weekly_lecture_expands_into_an_instance(monkeypatch, settings):
    """A recurring event must appear on the days it actually occurs."""
    start = (datetime.now(tz=settings.tz) + timedelta(hours=3)).replace(
        minute=0, second=0, microsecond=0
    )
    feed = ics(
        "BEGIN:VEVENT\r\n"
        "UID:lecture-1\r\n"
        f"DTSTART;TZID=Asia/Kolkata:{start:%Y%m%dT%H%M%S}\r\n"
        f"DTEND;TZID=Asia/Kolkata:{start + timedelta(hours=1):%Y%m%dT%H%M%S}\r\n"
        "RRULE:FREQ=WEEKLY\r\n"
        "SUMMARY:Data Structures Lecture\r\n"
        "LOCATION:Room 204\r\n"
        "END:VEVENT\r\n"
    )
    patch_feed(monkeypatch, feed)

    items = gcal.fetch_upcoming(settings)

    assert len(items) == 1
    assert items[0].summary == "Data Structures Lecture"
    assert items[0].location == "Room 204"
    assert items[0].all_day is False
    # The instance id must be unique per occurrence, not shared across the series.
    assert items[0].id.startswith("lecture-1@")


def test_all_day_event_is_flagged(monkeypatch, settings):
    tomorrow = (datetime.now(tz=settings.tz) + timedelta(days=1)).date()
    feed = ics(
        "BEGIN:VEVENT\r\n"
        "UID:holiday-1\r\n"
        f"DTSTART;VALUE=DATE:{tomorrow:%Y%m%d}\r\n"
        f"DTEND;VALUE=DATE:{tomorrow + timedelta(days=1):%Y%m%d}\r\n"
        "SUMMARY:University Holiday\r\n"
        "END:VEVENT\r\n"
    )
    patch_feed(monkeypatch, feed)

    items = gcal.fetch_upcoming(settings)

    assert len(items) == 1
    assert items[0].all_day is True
    assert items[0].start is not None and items[0].start.tzinfo is not None


def test_cancelled_events_are_skipped(monkeypatch, settings):
    start = datetime.now(tz=settings.tz) + timedelta(hours=2)
    feed = ics(
        "BEGIN:VEVENT\r\n"
        "UID:cancelled-1\r\n"
        f"DTSTART;TZID=Asia/Kolkata:{start:%Y%m%dT%H%M%S}\r\n"
        f"DTEND;TZID=Asia/Kolkata:{start + timedelta(hours=1):%Y%m%dT%H%M%S}\r\n"
        "SUMMARY:Cancelled Tutorial\r\n"
        "STATUS:CANCELLED\r\n"
        "END:VEVENT\r\n"
    )
    patch_feed(monkeypatch, feed)

    assert gcal.fetch_upcoming(settings) == []


def test_events_beyond_the_horizon_are_excluded(monkeypatch, settings):
    far = datetime.now(tz=settings.tz) + timedelta(days=9)
    feed = ics(
        "BEGIN:VEVENT\r\n"
        "UID:far-1\r\n"
        f"DTSTART;TZID=Asia/Kolkata:{far:%Y%m%dT%H%M%S}\r\n"
        f"DTEND;TZID=Asia/Kolkata:{far + timedelta(hours=1):%Y%m%dT%H%M%S}\r\n"
        "SUMMARY:Next week seminar\r\n"
        "END:VEVENT\r\n"
    )
    patch_feed(monkeypatch, feed)

    assert gcal.fetch_upcoming(settings) == []


def test_a_wrong_url_fails_loudly_rather_than_as_a_parser_error(monkeypatch, settings):
    """A reset iCal URL returns Google's HTML error page with HTTP 200."""
    patch_feed(monkeypatch, b"<!DOCTYPE html><html>Not found</html>")

    with pytest.raises(RuntimeError, match="did not return a calendar feed"):
        gcal.fetch_upcoming(settings)
