"""Made-up academy facts. They are the single source for two things:

1. `supabase/academy_test_schema.sql` seeds the TEST Supabase project from these lists
   (`seed_sql.py` writes the seed block; a test fails if the SQL file and this file disagree).
2. The academy hours and demo length that `find_demo_slots` uses.

EVERYTHING IN THIS FILE IS INVENTED. It is not real EduChess data and must never be copied
from it. The agent states fees, batches and event facts only from tool results (hard rule 2),
and the prompts contain no such facts.
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

# ---- rows seeded into the test Supabase project (one list per table) ----------------------
# Keys are the column names of the table with the same name in the SQL file.

PLANS = [
    {"name": "Starter (SYNTHETIC)", "classes_per_month": 4, "fee_rupees": 1234},
    {"name": "Regular (SYNTHETIC)", "classes_per_month": 8, "fee_rupees": 2222},
]

BATCHES = [
    {"name": "Batch A (SYNTHETIC)", "days": "Mon, Wed", "time_range": "17:00-18:00", "level": "beginner", "seats_left": 3},
    {"name": "Batch B (SYNTHETIC)", "days": "Sat", "time_range": "10:00-11:30", "level": "beginner", "seats_left": 5},
]

WORKSHOPS = [
    {"name": "Sample Weekend Workshop (SYNTHETIC)", "event_date": "2026-11-22", "fee_rupees": 150, "seats_left": 12},
]

TOURNAMENTS = [
    {"name": "Sample Rapid Tournament (SYNTHETIC)", "event_date": "2026-12-06", "entry_fee_rupees": 99, "seats_left": 20},
]
