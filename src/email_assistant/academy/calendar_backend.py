"""Calendar backends behind one small interface.

    GoogleCalendar  the real thing: Google Calendar API, on the TEST calendar named by GOOGLE_CALENDAR_ID
    FakeCalendar    in memory, for tests that run with no keys

The calendar tools (`calendar_tools.py`) only talk to `CalendarBackend`, so the same tests run on both.

What Google gets to see (hard rule 5, minimal child data):
- the event title "Demo class - <first name>", the start and the end;
- two hidden labels used to find the event again for reschedule/cancel: a short hash of the
  parent's email and the lower-case first name. The parent's email itself is not stored.
- no attendees and `sendUpdates="none"`: Google never emails anyone. Replies go through the outbox.

The only OAuth scope is `calendar.events` (read, create, change, delete events). Nothing else.
"""

import hashlib
import itertools
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from googleapiclient.errors import HttpError

from email_assistant.academy import config, settings

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
EVENT_KIND = "academy_demo"


class CalendarError(RuntimeError):
    """The calendar answered with an error. The message holds the status only, never event details."""


@dataclass(frozen=True)
class Block:
    """A stretch of time that is taken. `event_id` is None for blocks that are not our demo events."""

    start: datetime
    end: datetime
    event_id: str | None = None


@dataclass(frozen=True)
class DemoEvent:
    event_id: str
    start: datetime
    end: datetime


def parent_key(parent_email: str) -> str:
    """A short hash of the parent's email, used to find their event again (the email is not stored)."""
    return hashlib.sha256(parent_email.strip().lower().encode()).hexdigest()[:16]


def child_key(child_first_name: str) -> str:
    return child_first_name.strip().lower()


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True if two time ranges share any time. Touching ends (one ends when the other starts) do not."""
    return a_start < b_end and b_start < a_end


class CalendarBackend(ABC):
    source: str  # shown in every tool result, so evals can see where a fact came from

    @abstractmethod
    def busy(self, start: datetime, end: datetime) -> list[Block]:
        """Everything that takes time between start and end."""

    @abstractmethod
    def create_demo(self, summary: str, start: datetime, end: datetime, parent_email: str, child_first_name: str) -> DemoEvent:
        """Add one demo event."""

    @abstractmethod
    def find_demos(self, parent_email: str, child_first_name: str, after: datetime) -> list[DemoEvent]:
        """Demo events of this parent and child that have not ended yet."""

    @abstractmethod
    def move_demo(self, event_id: str, start: datetime, end: datetime) -> DemoEvent:
        """Change the time of one event."""

    @abstractmethod
    def delete_demo(self, event_id: str) -> None:
        """Remove one event."""


# ---- fake (tests) ------------------------------------------------------------------------------

class FakeCalendar(CalendarBackend):
    """A calendar in memory. Tests can look at `events` and `calls` afterwards."""

    source = "fake calendar (tests)"

    def __init__(self, busy: Iterable[tuple[datetime, datetime]] = ()):
        self._fixed = [Block(start, end) for start, end in busy]
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []  # names of the methods that were called, in order
        self._ids = itertools.count(1)

    def busy(self, start, end):
        self.calls.append("busy")
        blocks = list(self._fixed) + [Block(e["start"], e["end"], event_id) for event_id, e in self.events.items()]
        return [b for b in blocks if overlaps(b.start, b.end, start, end)]

    def create_demo(self, summary, start, end, parent_email, child_first_name):
        self.calls.append("create_demo")
        event_id = f"fake-{next(self._ids)}"
        self.events[event_id] = {"summary": summary, "start": start, "end": end,
                                 "parent": parent_key(parent_email), "child": child_key(child_first_name)}
        return DemoEvent(event_id, start, end)

    def find_demos(self, parent_email, child_first_name, after):
        self.calls.append("find_demos")
        found = [DemoEvent(i, e["start"], e["end"]) for i, e in self.events.items()
                 if e["parent"] == parent_key(parent_email) and e["child"] == child_key(child_first_name) and e["end"] > after]
        return sorted(found, key=lambda event: event.start)

    def move_demo(self, event_id, start, end):
        self.calls.append("move_demo")
        self.events[event_id].update(start=start, end=end)
        return DemoEvent(event_id, start, end)

    def delete_demo(self, event_id):
        self.calls.append("delete_demo")
        del self.events[event_id]


# ---- Google ------------------------------------------------------------------------------------

def _execute(request: Any) -> dict:
    """Run one Google request (the client library retries 429 and 5xx itself) and hide error details."""
    try:
        return request.execute(num_retries=3)
    except HttpError as exc:
        raise CalendarError(f"Google Calendar answered with HTTP {getattr(exc.resp, 'status', '?')}.") from None


def _event_moment(part: dict[str, str]) -> datetime:
    if "dateTime" in part:
        return datetime.fromisoformat(part["dateTime"]).astimezone(config.TIMEZONE)
    return datetime.fromisoformat(part["date"]).replace(tzinfo=config.TIMEZONE)  # all-day event: midnight India time


def _moment_body(moment: datetime) -> dict[str, str]:
    return {"dateTime": moment.astimezone(config.TIMEZONE).isoformat(), "timeZone": config.TIMEZONE_NAME}


class GoogleCalendar(CalendarBackend):
    """The Google Calendar API on one calendar. `service` is a googleapiclient Calendar v3 service."""

    source = "google calendar (test calendar)"

    def __init__(self, calendar_id: str, service: Any):
        self._id = calendar_id
        self._service = service

    @classmethod
    def from_env(cls) -> "GoogleCalendar":
        """Build from GOOGLE_CALENDAR_ID and the token file written by `google_auth`."""
        from googleapiclient.discovery import build

        calendar_id = settings.require_env(settings.CALENDAR_ID_VAR)
        creds = load_credentials()
        return cls(calendar_id, build("calendar", "v3", credentials=creds, cache_discovery=False))

    def _list(self, **params: Any) -> list[dict]:
        items: list[dict] = []
        token = None
        while True:
            call = dict(calendarId=self._id, singleEvents=True, orderBy="startTime", maxResults=250, **params)
            if token:
                call["pageToken"] = token
            page = _execute(self._service.events().list(**call))
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return items

    def busy(self, start, end):
        blocks = []
        for item in self._list(timeMin=start.isoformat(), timeMax=end.isoformat()):
            if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                continue  # cancelled, or marked "free"
            blocks.append(Block(_event_moment(item["start"]), _event_moment(item["end"]), item.get("id")))
        return blocks

    def create_demo(self, summary, start, end, parent_email, child_first_name):
        body = {
            "summary": summary,
            "start": _moment_body(start),
            "end": _moment_body(end),
            "extendedProperties": {"private": {
                "academy_kind": EVENT_KIND, "academy_parent": parent_key(parent_email), "academy_child": child_key(child_first_name),
            }},
        }
        created = _execute(self._service.events().insert(calendarId=self._id, body=body, sendUpdates="none"))
        return DemoEvent(created["id"], start, end)

    def find_demos(self, parent_email, child_first_name, after):
        items = self._list(
            timeMin=after.isoformat(),
            privateExtendedProperty=[f"academy_parent={parent_key(parent_email)}", f"academy_child={child_key(child_first_name)}"],
        )
        return [DemoEvent(i["id"], _event_moment(i["start"]), _event_moment(i["end"]))
                for i in items if i.get("status") != "cancelled"]

    def move_demo(self, event_id, start, end):
        body = {"start": _moment_body(start), "end": _moment_body(end)}
        _execute(self._service.events().patch(calendarId=self._id, eventId=event_id, body=body, sendUpdates="none"))
        return DemoEvent(event_id, start, end)

    def delete_demo(self, event_id):
        _execute(self._service.events().delete(calendarId=self._id, eventId=event_id, sendUpdates="none"))


def token_path() -> Path:
    return Path(settings.env_or(settings.TOKEN_FILE_VAR, settings.DEFAULT_TOKEN_FILE))


def credentials_path() -> Path:
    return Path(settings.env_or(settings.CREDENTIALS_FILE_VAR, settings.DEFAULT_CREDENTIALS_FILE))


def load_credentials():
    """Load the saved Google sign-in and refresh it if it ran out. Never opens a browser."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path()
    sign_in = "Run: python -m email_assistant.academy.google_auth"
    if not path.exists():
        raise settings.AcademyConfigError(f"No Google sign-in found at {path}. {sign_in}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise settings.AcademyConfigError(f"The Google sign-in at {path} cannot be refreshed. {sign_in}")
        try:
            creds.refresh(Request())
        except RefreshError:
            # Google "testing" mode ends refresh tokens after 7 days.
            raise settings.AcademyConfigError(f"The Google sign-in has expired. {sign_in}") from None
        path.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---- which backend the tools use -----------------------------------------------------------------

_override: CalendarBackend | None = None
_cached: CalendarBackend | None = None
_lock = threading.Lock()


def get_calendar() -> CalendarBackend:
    """The calendar the tools use: a test's fake if one is set, else Google (built once, on first use)."""
    global _cached
    if _override is not None:
        return _override
    with _lock:
        if _cached is None:
            _cached = GoogleCalendar.from_env()
        return _cached


@contextmanager
def use_calendar(backend: CalendarBackend) -> Iterator[CalendarBackend]:
    """Temporarily use another calendar (tests). A plain module variable, so MCP worker threads see it too."""
    global _override
    previous, _override = _override, backend
    try:
        yield backend
    finally:
        _override = previous

