"""Hard rule 2 in code: a number, date or time in a draft must be stated by the tool outputs of the same run."""

import pytest

from email_assistant.academy.grounding import extract, ungrounded

TOOL_OUTPUT = (
    '{"free_slots": ["2026-11-07T14:00", "2026-11-09T17:00"], "demo_minutes": 45, "demo_fee_rupees": 0, '
    '"plans": [{"name": "Starter", "classes_per_month": 4, "fee_rupees": 1234}], '
    '"batches": [{"days": "Mon, Wed", "time_range": "17:00-18:00", "seats_left": 3}], '
    '"workshops": [{"event_date": "2026-11-22", "fee_rupees": 150}]}'
)


@pytest.mark.parametrize("draft", [
    "The Starter plan has 4 classes a month for ₹1234.",
    "Fee: Rs. 1,234 per month.",                       # thousands comma
    "Your demo is on 2026-11-07 at 14:00.",
    "Your demo is on Saturday 7 Nov at 2 pm.",         # same date and time, written another way
    "Saturday, November 7, 2026 at 2:00 PM",
    "Monday 9th November, 5 pm - 6 pm, is free.",      # 5 pm = 17:00, 6 pm = 18:00 (end of the batch)
    "Classes run 5-6 pm on Mon, Wed.",                 # one am/pm for the whole range
    "The demo is free (₹0) and lasts 45 minutes.",
    "The workshop is on 22 November 2026 and costs ₹150.",
    "ఫీజు ౧౨౩౪",                                        # Telugu digits = 1234
    "Hello Arjun, thank you for writing. Regards, Coach",  # no numbers at all
    "",
])
def test_numbers_that_the_tool_outputs_state_pass(draft):
    assert ungrounded(draft, TOOL_OUTPUT) == []


@pytest.mark.parametrize("draft,expected", [
    ("The fee is ₹500 per month.", ["500"]),                       # invented fee
    ("The fee is ₹1235 per month.", ["1235"]),                     # one digit off
    ("Only 7 seats are left.", ["7"]),                             # invented seat count
    ("The workshop is on 23 November.", ["23 November"]),          # wrong day
    ("The workshop is on 22 December 2026.", ["22 December 2026"]),  # wrong month
    ("Your demo is at 3 pm.", ["3 pm"]),                           # a time no tool gave
    ("Your demo is at 2 am.", ["2 am"]),                           # 02:00, not 14:00
    ("It costs ₹1,000 a year.", ["1,000"]),
    ("We have 12 classes and a 10% discount.", ["12", "10"]),      # two invented numbers
    ("Join on 2027-01-05.", ["2027-01-05"]),
])
def test_numbers_that_no_tool_stated_are_reported(draft, expected):
    assert ungrounded(draft, TOOL_OUTPUT) == expected


def test_an_empty_source_backs_up_nothing():
    assert ungrounded("Fee is ₹1234", "") == ["1234"]


def test_a_date_component_is_not_a_free_pass_for_a_plain_number():
    """22 appears in the source only as part of 2026-11-22. '22 seats' must not pass because of it."""
    assert ungrounded("22 seats are left", TOOL_OUTPUT) == ["22"]


def test_a_bare_time_is_accepted_if_either_reading_is_stated():
    assert ungrounded("see you at 5:00", TOOL_OUTPUT) == []      # 17:00 is stated
    assert ungrounded("see you at 8:30", TOOL_OUTPUT) == ["8:30"]


def test_a_date_with_no_year_in_the_source_backs_a_date_with_a_year():
    assert ungrounded("on 22 November 2026", "Workshop on 22 Nov.") == []


@pytest.mark.parametrize("text,kinds", [
    ("2026-11-07T14:00", ["date", "time"]),
    ("22nd of Nov", ["day_month"]),
    ("Nov 22, 2026", ["date"]),
    ("5 pm to 6 pm", ["time", "time"]),
    ("4.50 and 1,234", ["number", "number"]),
    ("31 February", ["day_month"]),                   # not checked for real calendar days; only 1-31 is required
    ("45 minutes", ["number"]),                       # 'minutes' is not read as 'am'
    ("May I ask? 5 may", ["day_month"]),
])
def test_what_is_read_from_text(text, kinds):
    assert [f.kind for f in extract(text)] == kinds


def test_an_impossible_clock_reading_is_not_a_time():
    assert [f.kind for f in extract("13 pm")] == ["number"]
