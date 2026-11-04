"""Mock tools for Phase 1. They read made-up facts from `config.py` and change nothing.

The names match the real tools of Phase 2 (`find_demo_slots`, `create_demo_event`,
`send_reply`, ...) so labelled messages written now stay valid later.

Date and time arguments are plain strings, never `datetime`. Groq checks a `datetime`
argument as a strict RFC 3339 date-time and rejects a model's "2026-11-07T14:00" (no seconds,
no time zone). Strings are parsed in code instead, with `parse_local_datetime`.
"""

import json
from datetime import date, datetime, timedelta

from langchain_core.tools import tool

from email_assistant.academy import config
from email_assistant.tools.default.email_tools import Done, Question  # noqa: F401  (re-exported)

SOURCE = "mock (synthetic data)"


def parse_local_datetime(value: str) -> datetime:
    """Read "2026-11-07T14:00", "2026-11-07 14:00" or with seconds, as India time."""
    text = value.strip().replace(" ", "T", 1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=config.TIMEZONE)


def _parse_day(value: str) -> date:
    return date.fromisoformat(value.strip()[:10])


def _as_json(payload: dict) -> str:
    return json.dumps({"source": SOURCE, **payload}, ensure_ascii=False)


# ---- read-only tools (no approval) ---------------------------------------------------------

@tool
def find_demo_slots(date_from: str, date_to: str) -> str:
    """List free demo-class slots from date_from to date_to. Both are dates as YYYY-MM-DD."""
    try:
        start, end = _parse_day(date_from), _parse_day(date_to)
    except ValueError:
        return _as_json({"error": "Dates must look like YYYY-MM-DD."})
    slots = []
    day = start
    while day <= min(end, start + timedelta(days=31)):
        for time in config.DEMO_SLOT_TIMES.get(day.strftime("%A"), []):
            slots.append(f"{day.isoformat()}T{time}")
        day += timedelta(days=1)
    return _as_json({"timezone": config.TIMEZONE_NAME, "demo_minutes": config.DEMO_CLASS_MINUTES,
                     "demo_fee_rupees": config.DEMO_CLASS_FEE_RUPEES, "free_slots": slots})


@tool
def get_fee_plans() -> str:
    """Get the fee plans: name, classes per month, monthly fee in rupees."""
    return _as_json({"fee_plans": config.FEE_PLANS})


@tool
def get_batches() -> str:
    """Get the regular batches: days, time, level and seats left."""
    return _as_json({"batches": config.BATCHES})


@tool
def get_upcoming_events() -> str:
    """Get upcoming workshops and tournaments: name, date and entry fee in rupees."""
    return _as_json({"events": config.EVENTS})


# ---- write tools (the owner must approve every call) ---------------------------------------

@tool
def create_demo_event(parent_email: str, child_first_name: str, start: str, end: str) -> str:
    """Book a demo class on the calendar. start and end look like 2026-11-07T14:00 (India time)."""
    try:
        begins, ends = parse_local_datetime(start), parse_local_datetime(end)
    except ValueError:
        return _as_json({"error": "start and end must look like 2026-11-07T14:00."})
    return _as_json({"booked": True, "child": child_first_name, "parent_email": parent_email,
                     "start": begins.isoformat(), "end": ends.isoformat()})


@tool
def send_reply(to: str, subject: str, body: str) -> str:
    """Send a reply to the parent."""
    return _as_json({"sent": True, "to": to, "subject": subject})


ACADEMY_TOOLS = [
    find_demo_slots, get_fee_plans, get_batches, get_upcoming_events,
    create_demo_event, send_reply, Question, Done,
]

TOOLS_PROMPT = """
1. find_demo_slots(date_from, date_to) - free demo-class slots between two dates (YYYY-MM-DD). Read-only.
2. get_fee_plans() - the fee plans and their prices. Read-only.
3. get_batches() - the regular batches: days, time, level, seats left. Read-only.
4. get_upcoming_events() - upcoming workshops and tournaments. Read-only.
5. create_demo_event(parent_email, child_first_name, start, end) - book a demo class. start and end are strings like 2026-11-07T14:00. The owner approves first.
6. send_reply(to, subject, body) - reply to the parent. The owner approves first.
7. Question(content) - ask the owner when you are missing information a tool cannot give.
8. Done - the task is finished.
"""
