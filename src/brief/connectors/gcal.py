"""Calendar connector via Google Calendar's secret iCal address.

Google Calendar publishes each calendar at a private, signed .ics URL that
needs no OAuth and never expires. That URL *is* the credential: anyone holding
it can read your calendar, so it belongs in secrets alongside the app password.
If it ever leaks, reset it from the same settings page that issued it.

Recurring events are expanded client-side, because an .ics feed ships RRULEs
rather than instances - a weekly 9am lecture arrives as one event with a
repeat rule, not thirty events.

Returns the same `CalendarItem` the API version did, so nothing downstream
changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx

from brief.config import Settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CalendarItem:
    id: str
    summary: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    location: str
    description: str
    calendar: str

    def as_triage_line(self) -> str:
        if self.all_day:
            when = f"{self.start:%a %d %b} (all day)" if self.start else "undated"
        elif self.start:
            when = f"{self.start:%a %H:%M}"
            if self.end:
                when += f"-{self.end:%H:%M}"
        else:
            when = "undated"
        line = f"[{self.id}] {when} | {self.summary}"
        if self.location:
            line += f" | at {self.location}"
        if self.description:
            line += f"\n  notes: {self.description[:200]}"
        return line


def _as_datetime(value, tz) -> tuple[datetime | None, bool]:
    """Normalise an icalendar DTSTART/DTEND into a tz-aware datetime.

    An all-day event carries a `date`; a timed event carries a `datetime` that
    may be naive. Both are coerced to the local zone so downstream comparisons
    against `now` are safe.
    """
    if value is None:
        return None, False
    # datetime is a subclass of date, so check the narrower type first.
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz), True
    return None, False


def fetch_upcoming(
    settings: Settings,
    *,
    horizon_hours: int = 30,
    max_results: int = 40,
) -> list[CalendarItem]:
    """Events from now through the horizon.

    The default horizon runs slightly past 24h so a 7am brief still catches
    tomorrow-morning commitments worth preparing for tonight.
    """
    settings.require("calendar_ical_url")

    import recurring_ical_events
    from icalendar import Calendar

    tz = settings.tz
    now = datetime.now(tz=tz)
    horizon = now + timedelta(hours=horizon_hours)

    response = httpx.get(settings.calendar_ical_url, timeout=60, follow_redirects=True)
    response.raise_for_status()

    # A wrong or reset URL returns Google's HTML error page with a 200, which
    # would otherwise surface as a confusing parser error.
    body = response.content
    if not body.lstrip().startswith(b"BEGIN:VCALENDAR"):
        raise RuntimeError(
            "The iCal URL did not return a calendar feed. Check that you copied "
            "the *secret* address in iCal format, and that it has not been reset."
        )

    calendar = Calendar.from_ical(body)
    calendar_name = str(calendar.get("X-WR-CALNAME", "primary"))

    occurrences = recurring_ical_events.of(calendar).between(now, horizon)
    log.info("ical: %d occurrences in the next %dh", len(occurrences), horizon_hours)

    items: list[CalendarItem] = []
    for event in occurrences:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue

        start, all_day = _as_datetime(_value(event, "DTSTART"), tz)
        end, _ = _as_datetime(_value(event, "DTEND"), tz)

        items.append(
            CalendarItem(
                # Recurring instances share a UID, so the start time
                # disambiguates one occurrence from the next.
                id=f"{event.get('UID', '')}@{start:%Y%m%dT%H%M}" if start else str(event.get("UID", "")),
                summary=str(event.get("SUMMARY", "(untitled)")).strip(),
                start=start,
                end=end,
                all_day=all_day,
                location=str(event.get("LOCATION", "")).strip(),
                description=str(event.get("DESCRIPTION", "")).strip(),
                calendar=calendar_name,
            )
        )

    items.sort(key=lambda i: (i.start is None, i.start))
    return items[:max_results]


def _value(event, key: str):
    """icalendar wraps values; `.dt` holds the date or datetime."""
    prop = event.get(key)
    return getattr(prop, "dt", None) if prop is not None else None


__all__ = ["CalendarItem", "fetch_upcoming"]
