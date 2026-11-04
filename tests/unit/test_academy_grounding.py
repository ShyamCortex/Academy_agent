"""Hard rule 2 in code: a number, date or time in a draft must be stated by the tool outputs of the same run."""

import pytest

from email_assistant.academy.grounding import extract, ungrounded, wrong_weekdays

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


# ---- equivalent formats count as the same fact ---------------------------------------------------------------------

# What find_demo_slots returns now: each slot carries its weekday, date and 24 h time.
SLOT_OUTPUT = (
    '{"demo_minutes": 45, "demo_terms": "free, 45 minutes", "free_slots": ['
    '{"label": "Saturday 2026-11-07 10:00 IST", "start": "2026-11-07T10:00", "end": "2026-11-07T10:45"}, '
    '{"label": "Monday 2026-11-09 18:00 IST", "start": "2026-11-09T18:00", "end": "2026-11-09T18:45"}, '
    '{"label": "Wednesday 2026-11-11 17:00 IST", "start": "2026-11-11T17:00", "end": "2026-11-11T17:45"}]}'
)


@pytest.mark.parametrize("draft", [
    "Monday 2026-11-09 18:00 IST",                        # the tool's own label
    "Monday, 9 November at 6 pm",
    "Mon 9th Nov, 6:00 PM",
    "6 p.m. on Monday 9 Nov",
    "Monday 6pm on 9 Nov",
    "Monday 9 Nov, 5.00 pm to 6.00 pm",                  # 17:00 and 18:00 are both stated (slot start and end of the 17:00 slot)
    "Saturday 7th at 10 am",                              # weekday + day of the month = 2026-11-07
    "Saturday the 7th, 10:00",
    "on the 7th and the 9th",                             # only the day of the month: matches the dates the tool gave
    "7 Nov (Saturday), 10 am",
    "Wednesday 11 Nov 5 pm, 17:00",
    "Free, 45 minutes",
    "Dates: 7th and 9th Nov, 10 am",                     # a list of two days
])
def test_the_same_slot_written_another_way_is_grounded_and_has_the_right_weekday(draft):
    assert ungrounded(draft, SLOT_OUTPUT) == [] and wrong_weekdays(draft, SLOT_OUTPUT) == []


@pytest.mark.parametrize("draft,expected", [
    ("Monday 2026-11-16 18:00 IST", ["2026-11-16"]),      # a Monday, but a date the tool never returned
    ("Monday 16 Nov at 6 pm", ["16 Nov"]),
    ("Saturday 14th at 10 am", ["Saturday 14th"]),       # a day of the month the tool never returned
    ("on the 8th", ["8th"]),
    ("Monday 9 Nov at 7 pm", ["7 pm"]),                   # the right date, a time the tool never returned
    ("Monday 9 Nov at 6:30 pm", ["6:30 pm"]),
    ("Saturday 7 Nov, 10 am, ₹300", ["300"]),            # a made-up fee next to real slots
    ("45 minutes, 3 seats left", ["3"]),
])
def test_numbers_dates_and_times_the_tools_never_returned_still_fail_in_every_format(draft, expected):
    assert ungrounded(draft, SLOT_OUTPUT) == expected


# ---- a weekday next to a date must be that date's weekday -----------------------------------------------------------

@pytest.mark.parametrize("draft,expected", [
    ("Tuesday, 7 Nov at 10 am", ["Tuesday, 7 Nov"]),                 # v1 eval: 7 Nov 2026 is a Saturday
    ("Wednesday, 9 Nov 5:00 pm", ["Wednesday, 9 Nov"]),             # v1 eval: 9 Nov 2026 is a Monday
    ("Monday demo on 2026-11-07", []),                             # NOT next to the date: out of reach of this check (see the module's limits)
    ("Monday 2026-11-07 10:00 IST", ["Monday 2026-11-07"]),         # the tool's label format with the wrong weekday
    ("Sun 2026-11-09", ["Sun 2026-11-09"]),
    ("7 Nov (Sunday)", ["7 Nov (Sunday)"]),                         # weekday after the date, in brackets
    ("7 Nov, Monday", ["7 Nov, Monday"]),                           # weekday after the date, after a comma
    ("Tuesday 7th", ["Tuesday 7th"]),                               # weekday + day of the month
    ("Saturday the 9th", ["Saturday the 9th"]),
    ("Saturday 7 Nov, Sunday 9 Nov", ["Sunday 9 Nov"]),             # one right, one wrong
    ("Monday 2026-02-30", ["Monday 2026-02-30"]),                   # no such date
    ("Thurs 7 November 2026", ["Thurs 7 November 2026"]),
    ("Fri, Nov 9", ["Fri, Nov 9"]),
])
def test_a_wrong_weekday_next_to_a_date_is_reported(draft, expected):
    assert wrong_weekdays(draft, SLOT_OUTPUT) == expected


@pytest.mark.parametrize("draft", [
    "Saturday 7 Nov, Monday 9 Nov and Wednesday 11 Nov",            # a list: each weekday belongs to the date after it, not the one before
    "7 Nov, Monday 9 Nov",                                          # the first date has no weekday of its own: 'Monday' is not its weekday
    "7 Nov; Monday the 9th",
    "Monday, Wednesday and Saturday",                               # weekdays with no date
    "Mon, Wed 5-6 pm",                                              # batch days and a time range
    "Batches run on Saturday; the next one starts 7 Nov",           # not next to each other
    "Sat 10 am",                                                    # a weekday and a time
    "Sunday",
    "",
])
def test_weekdays_that_are_not_next_to_a_date_are_not_judged(draft):
    assert wrong_weekdays(draft, SLOT_OUTPUT) == []


def test_a_weekday_with_a_date_that_has_no_year_is_checked_against_the_dates_the_source_states():
    assert wrong_weekdays("Tuesday, 7 Nov", SLOT_OUTPUT) == ["Tuesday, 7 Nov"]
    assert wrong_weekdays("Tuesday, 7 Nov", "no dates here") == []           # nothing to compare with; `ungrounded` reports the date
    assert ungrounded("Tuesday, 7 Nov", "no dates here") == ["7 Nov"]
    assert wrong_weekdays("Tuesday, 7 Nov", "Workshop on 2025-11-04") == []  # 7 Nov is not a date the source states


def test_the_weekday_of_a_date_with_a_year_needs_no_source_at_all():
    assert wrong_weekdays("Saturday 2026-11-07", "") == []
    assert wrong_weekdays("Sunday 2026-11-07", "") == ["Sunday 2026-11-07"]


def test_weekday_names_are_read_in_any_case_and_abbreviation():
    for name in ("MONDAY", "monday", "Mon", "mon.", "Tues", "Thurs"):
        assert [f.weekday is not None for f in extract(f"{name} 2026-11-09") if f.kind == "date"] == [True]


def test_a_weekday_inside_another_word_is_not_a_weekday():
    assert wrong_weekdays("Thursday 2026-11-05 and Satisfaction 2026-11-06", "") == []   # 'Sat' in 'Satisfaction'; 5 Nov 2026 is a Thursday
    assert all(f.weekday is None for f in extract("Satisfaction 2026-11-06"))


def test_what_is_read_from_text_with_weekdays():
    assert [(f.kind, f.weekday) for f in extract("Saturday 7th at 10 am")] == [("weekday_day", 5), ("time", None)]
    assert [(f.kind, f.weekday) for f in extract("the 7th")] == [("day_of_month", None)]
    assert [f.kind for f in extract("Monday 9 Nov at 6 pm")] == ["day_month", "time"]
    assert [f.written for f in extract("Monday, 9 Nov")] == ["Monday, 9 Nov"]
    assert [f.label for f in extract("Monday, 9 Nov")] == ["9 Nov"]            # ungrounded() keeps reporting the date alone


# ---- several days that share a month ------------------------------------------------------------------------------

DATES_OUTPUT = "Saturdays: 2026-11-07, 2026-11-14, 2026-11-21"


@pytest.mark.parametrize("draft", ["7, 14 and 21 November", "7th, 14th & 21st Nov", "Nov: 7, 14, 21 November 2026", "7 and 14 of November"])
def test_a_list_of_days_with_one_month_is_read_as_dates_that_the_source_states(draft):
    assert ungrounded(draft, DATES_OUTPUT) == []


def test_a_list_of_days_reports_only_the_days_the_source_does_not_state():
    assert ungrounded("7, 14, 21 and 28 November", DATES_OUTPUT) == ["28 November"]
    assert ungrounded("7, 14, 21 and 28 November 2026", DATES_OUTPUT) == ["28 November"]
    assert ungrounded("7, 14 and 21 December", DATES_OUTPUT) == ["7 December", "14 December", "21 December"]


def test_a_list_of_days_is_read_as_days_only_when_a_month_follows():
    assert [f.kind for f in extract("7, 14 and 21")] == ["number", "number", "number"]
    assert [f.kind for f in extract("4, 8 classes")] == ["number", "number"]
    assert [(f.kind, f.value) for f in extract("8 classes and 22 November")] == [("day_month", (11, 22)), ("number", "8")]   # dates are read first
    assert [f.kind for f in extract("10:00 and 11:30")] == ["time", "time"]
    assert [(f.kind, f.value) for f in extract("32, 5 Nov")] == [("day_month", (11, 5)), ("number", "32")]   # 32 is not a day: no list
