"""Calendar tools. One read-only tool, three write tools.

    find_demo_slots   read-only  -> may run without the owner
    create_demo_event write      -> the owner approves every call (not on the allow-list)
    reschedule_demo   write      -> same
    cancel_demo       write      -> same

All dates and times are plain strings in India time and are parsed in code (`dates.py`).
Each write tool checks its input again in code, whatever the model was told in the prompt.
Results always carry a `source` field. Errors come back as `{"error": ...}` so the model can
ask the owner (Question tool) instead of guessing.
"""

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from langchain_core.tools import tool

from email_assistant.academy import config, dates
from email_assistant.academy.calendar_backend import Block, CalendarBackend, CalendarError, get_calendar, overlaps

MAX_RANGE_DAYS = 31  # find_demo_slots looks at most this far ahead of date_from
MAX_EVENT_MINUTES = 120  # a demo is 45 minutes; anything above this is a typo or a mistake


def _json(calendar: CalendarBackend, payload: dict[str, Any]) -> str:
    return json.dumps({"source": calendar.source, **payload}, ensure_ascii=False)


def free_slots(busy: Iterable[Block], day_from: date, day_to: date, now: datetime) -> list[datetime]:
    """Academy demo start times between two days (inclusive) that are in the future and not taken.

    A slot is taken if the demo (start to start + demo length) overlaps anything in `busy`.
    """
    busy = list(busy)
    length = timedelta(minutes=config.DEMO_CLASS_MINUTES)
    slots = []
    day = day_from
    while day <= min(day_to, day_from + timedelta(days=MAX_RANGE_DAYS)):
        for hhmm in config.DEMO_SLOT_TIMES.get(dates.weekday_name(day), []):
            hour, minute = (int(part) for part in hhmm.split(":"))
            start = datetime.combine(day, time(hour, minute), tzinfo=config.TIMEZONE)
            if start > now and not any(overlaps(start, start + length, b.start, b.end) for b in busy):
                slots.append(start)
        day += timedelta(days=1)
    return slots


def _first_name(value: str) -> str:
    """Keep only the first name (minimal child data)."""
    words = value.strip().split()
    return words[0][:40] if words else ""


def _bad_event_input(parent_email: str, first_name: str) -> str | None:
    if "@" not in parent_email or " " in parent_email.strip():
        return "parent_email must be the parent's email address."
    if not first_name:
        return "child_first_name is needed."
    return None


def _bad_window(start: datetime, end: datetime, now: datetime) -> str | None:
    if end <= start:
        return "end must be after start."
    if end - start > timedelta(minutes=MAX_EVENT_MINUTES):
        return f"A demo class is {config.DEMO_CLASS_MINUTES} minutes. The end is too far after the start."
    if start <= now:
        return "That time has already passed."
    return None


def _taken(calendar: CalendarBackend, start: datetime, end: datetime, ignore_event: str | None = None) -> bool:
    """True if anything else takes time between start and end. `ignore_event` is the event being moved."""
    return any(
        overlaps(b.start, b.end, start, end) and (ignore_event is None or b.event_id != ignore_event)
        for b in calendar.busy(start, end)
    )


# ---- read-only ------------------------------------------------------------------------------

@tool
def find_demo_slots(date_from: str, date_to: str) -> str:
    """List free demo-class slots from date_from to date_to. Both are dates as YYYY-MM-DD."""
    calendar = get_calendar()
    try:
        day_from, day_to = dates.parse_day(date_from), dates.parse_day(date_to)
    except ValueError:
        return _json(calendar, {"error": "Dates must look like YYYY-MM-DD."})
    if day_to < day_from:
        return _json(calendar, {"error": "date_to must not be before date_from."})
    day_to = min(day_to, day_from + timedelta(days=MAX_RANGE_DAYS))
    window_start = datetime.combine(day_from, time.min, tzinfo=config.TIMEZONE)
    window_end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=config.TIMEZONE)
    try:
        busy = calendar.busy(window_start, window_end)
    except CalendarError as exc:
        return _json(calendar, {"error": str(exc)})
    slots = free_slots(busy, day_from, day_to, dates.now_ist())
    return _json(calendar, {
        "timezone": config.TIMEZONE_NAME,
        "demo_minutes": config.DEMO_CLASS_MINUTES,
        "demo_fee_rupees": config.DEMO_CLASS_FEE_RUPEES,
        "checked_from": day_from.isoformat(),
        "checked_to": day_to.isoformat(),
        "free_slots": [dates.iso_minutes(s) for s in slots],
    })


# ---- writes (the owner must approve every call) -----------------------------------------------

@tool
def create_demo_event(parent_email: str, child_first_name: str, start: str, end: str) -> str:
    """Book a demo class on the calendar. start and end are strings like 2026-11-07T14:00 (India time)."""
    calendar = get_calendar()
    first = _first_name(child_first_name)
    problem = _bad_event_input(parent_email, first)
    if problem is None:
        try:
            begins, ends = dates.parse_local_datetime(start), dates.parse_local_datetime(end)
        except ValueError:
            problem = "start and end must look like 2026-11-07T14:00."
        else:
            problem = _bad_window(begins, ends, dates.now_ist())
    if problem:
        return _json(calendar, {"booked": False, "error": problem})
    try:
        if calendar.find_demos(parent_email, first, dates.now_ist()):
            return _json(calendar, {"booked": False, "error": "This child already has a demo booked. Use reschedule_demo to move it."})
        if _taken(calendar, begins, ends):
            return _json(calendar, {"booked": False, "error": "That time is already taken. Call find_demo_slots and pick a free slot."})
        event = calendar.create_demo(f"Demo class - {first}", begins, ends, parent_email, first)
    except CalendarError as exc:
        return _json(calendar, {"booked": False, "error": str(exc)})
    return _json(calendar, {"booked": True, "event_id": event.event_id, "child": first,
                            "start": dates.iso_minutes(event.start), "end": dates.iso_minutes(event.end)})


def _only_demo(calendar: CalendarBackend, parent_email: str, first: str):
    """The one upcoming demo of this parent and child, or an error text."""
    found = calendar.find_demos(parent_email, first, dates.now_ist())
    if not found:
        return None, "No upcoming demo is booked for this parent and child."
    if len(found) > 1:
        return None, "More than one demo is booked for this parent and child. Ask the owner which one (Question tool)."
    return found[0], None


@tool
def reschedule_demo(parent_email: str, child_first_name: str, new_start: str, new_end: str) -> str:
    """Move a booked demo class to a new time. new_start and new_end are strings like 2026-11-07T14:00 (India time)."""
    calendar = get_calendar()
    first = _first_name(child_first_name)
    problem = _bad_event_input(parent_email, first)
    if problem is None:
        try:
            begins, ends = dates.parse_local_datetime(new_start), dates.parse_local_datetime(new_end)
        except ValueError:
            problem = "new_start and new_end must look like 2026-11-07T14:00."
        else:
            problem = _bad_window(begins, ends, dates.now_ist())
    if problem:
        return _json(calendar, {"rescheduled": False, "error": problem})
    try:
        event, problem = _only_demo(calendar, parent_email, first)
        if problem:
            return _json(calendar, {"rescheduled": False, "error": problem})
        if _taken(calendar, begins, ends, ignore_event=event.event_id):
            return _json(calendar, {"rescheduled": False, "error": "That time is already taken. Call find_demo_slots and pick a free slot."})
        moved = calendar.move_demo(event.event_id, begins, ends)
    except CalendarError as exc:
        return _json(calendar, {"rescheduled": False, "error": str(exc)})
    return _json(calendar, {"rescheduled": True, "event_id": moved.event_id, "child": first,
                            "old_start": dates.iso_minutes(event.start),
                            "start": dates.iso_minutes(moved.start), "end": dates.iso_minutes(moved.end)})


@tool
def cancel_demo(parent_email: str, child_first_name: str) -> str:
    """Cancel a booked demo class. It is found by the parent's email and the child's first name."""
    calendar = get_calendar()
    first = _first_name(child_first_name)
    problem = _bad_event_input(parent_email, first)
    if problem:
        return _json(calendar, {"cancelled": False, "error": problem})
    try:
        event, problem = _only_demo(calendar, parent_email, first)
        if problem:
            return _json(calendar, {"cancelled": False, "error": problem})
        calendar.delete_demo(event.event_id)
    except CalendarError as exc:
        return _json(calendar, {"cancelled": False, "error": str(exc)})
    return _json(calendar, {"cancelled": True, "child": first, "was_start": dates.iso_minutes(event.start)})


CALENDAR_READ_TOOLS = [find_demo_slots]
CALENDAR_WRITE_TOOLS = [create_demo_event, reschedule_demo, cancel_demo]
