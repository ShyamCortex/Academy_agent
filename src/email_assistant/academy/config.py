"""Made-up academy facts for Phase 1.

EVERYTHING IN THIS FILE IS INVENTED. It is a stand-in for the real academy so the mock tools,
prompts and tests have something to read. It is not real EduChess data and must never be
copied from it. In Phase 2 the mock tools are replaced by tools that read the real data, and
the graph then states facts only from tool results (hard rule 2).

The prompts contain no fees, dates or seat counts. Only the mock tools in `tools.py` read those
facts, so such a fact can reach a reply only by way of a tool result. (The sign-off below is the
only text from this file that the prompts use.)
"""

from zoneinfo import ZoneInfo

ACADEMY_NAME = "Example Chess Academy (SYNTHETIC)"
OWNER_NAME = "Coach Sample"
OWNER_SIGN_OFF = "Regards,\nCoach Sample\nExample Chess Academy"

# All dates and times the agent uses are in this zone (India has no daylight saving).
TIMEZONE_NAME = "Asia/Kolkata"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

DEMO_CLASS_MINUTES = 45
DEMO_CLASS_FEE_RUPEES = 0

# Free slots a demo can be booked into: weekday -> start times (24 h clock, IST).
DEMO_SLOT_TIMES = {
    "Monday": ["17:00", "18:00"],
    "Wednesday": ["17:00"],
    "Saturday": ["10:00", "11:30"],
}

FEE_PLANS = [
    {"plan": "Starter (SYNTHETIC)", "classes_per_month": 4, "fee_rupees": 1234},
    {"plan": "Regular (SYNTHETIC)", "classes_per_month": 8, "fee_rupees": 2222},
]

BATCHES = [
    {"batch": "Batch A (SYNTHETIC)", "days": "Mon, Wed", "time": "17:00-18:00", "level": "beginner", "seats_left": 3},
    {"batch": "Batch B (SYNTHETIC)", "days": "Sat", "time": "10:00-11:30", "level": "beginner", "seats_left": 5},
]

EVENTS = [
    {"event": "Sample Rapid Tournament (SYNTHETIC)", "date": "2026-12-06", "entry_fee_rupees": 99},
]
