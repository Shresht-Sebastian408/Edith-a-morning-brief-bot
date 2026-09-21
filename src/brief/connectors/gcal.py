"""Google Calendar read-only connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from brief.config import Settings
from brief.connectors.google_auth import build_service


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


def _parse_point(point: dict, tz) -> tuple[datetime | None, bool]:
    """Calendar returns either `dateTime` (timed) or `date` (all-day)."""
    if "dateTime" in point:
        return datetime.fromisoformat(point["dateTime"]).astimezone(tz), False
    if "date" in point:
        day = date.fromisoformat(point["date"])
        return datetime.combine(day, time.min, tzinfo=tz), True
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
    tz = settings.tz
    now = datetime.now(tz=tz)
    service = build_service("calendar", "v3", settings)

    response = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(hours=horizon_hours)).isoformat(),
            singleEvents=True,     # expand recurring events into instances
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )

    items: list[CalendarItem] = []
    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue
        # Skip events you have actively declined.
        if any(
            a.get("self") and a.get("responseStatus") == "declined"
            for a in event.get("attendees", [])
        ):
            continue

        start, all_day = _parse_point(event.get("start", {}), tz)
        end, _ = _parse_point(event.get("end", {}), tz)

        items.append(
            CalendarItem(
                id=event.get("id", ""),
                summary=(event.get("summary") or "(untitled)").strip(),
                start=start,
                end=end,
                all_day=all_day,
                location=(event.get("location") or "").strip(),
                description=(event.get("description") or "").strip(),
                calendar=response.get("summary", "primary"),
            )
        )
    return items


__all__ = ["CalendarItem", "fetch_upcoming"]
