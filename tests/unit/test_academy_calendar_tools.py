"""Calendar tools on a fake calendar: slot finder, booking, rescheduling, cancelling. No keys, no network."""

import json
from datetime import datetime

import pytest

from academy_helpers import NOW
from email_assistant.academy import config, dates
from email_assistant.academy.calendar_backend import Block, CalendarError, FakeCalendar, parent_key, use_calendar
from email_assistant.academy.calendar_tools import (
    cancel_demo, create_demo_event, find_demo_slots, free_slots, reschedule_demo,
)

PARENT = "parent.a@example.invalid"


def at(day: str, hhmm: str) -> datetime:
    return dates.parse_local_datetime(f"{day}T{hhmm}")


def slots(world, date_from="2026-11-04", date_to="2026-11-09") -> list[str]:
    return json.loads(find_demo_slots.invoke({"date_from": date_from, "date_to": date_to}))["free_slots"]


def book(day="2026-11-07", start="10:00", end="10:45", email=PARENT, child="Arjun") -> dict:
    return json.loads(create_demo_event.invoke(
        {"parent_email": email, "child_first_name": child, "start": f"{day}T{start}", "end": f"{day}T{end}"}))


# ---- find_demo_slots -----------------------------------------------------------------------------

def test_slots_follow_the_academy_hours_on_an_empty_calendar(academy_world):
    assert slots(academy_world) == [
        "2026-11-04T17:00",                       # Wednesday (it is 10:00 now, so 17:00 is still ahead)
        "2026-11-07T10:00", "2026-11-07T11:30",   # Saturday
        "2026-11-09T17:00", "2026-11-09T18:00",   # Monday
    ]


def test_every_slot_is_an_academy_slot():
    found = free_slots([], dates.parse_day("2026-11-01"), dates.parse_day("2026-12-31"), NOW)
    assert found and all(s.strftime("%H:%M") in config.DEMO_SLOT_TIMES[dates.weekday_name(s.date())] for s in found)
    assert all(s > NOW for s in found)


def test_a_busy_block_removes_exactly_the_slots_it_overlaps(academy_world):
    academy_world.calendar._fixed = [Block(at("2026-11-07", "10:30"), at("2026-11-07", "11:00"))]
    assert "2026-11-07T10:00" not in slots(academy_world)   # the 10:00-10:45 demo would overlap 10:30-11:00
    assert "2026-11-07T11:30" in slots(academy_world)


def test_a_block_that_ends_when_a_slot_starts_does_not_remove_it(academy_world):
    academy_world.calendar._fixed = [Block(at("2026-11-07", "09:00"), at("2026-11-07", "10:00"))]
    assert "2026-11-07T10:00" in slots(academy_world)


def test_slots_in_the_past_are_never_offered(academy_world, monkeypatch):
    monkeypatch.setattr(dates, "now_ist", lambda: at("2026-11-04", "17:30"))
    assert "2026-11-04T17:00" not in slots(academy_world)
    assert "2026-11-07T10:00" in slots(academy_world)


def test_a_booked_demo_is_no_longer_offered(academy_world):
    assert book()["booked"] is True
    assert "2026-11-07T10:00" not in slots(academy_world)


def test_bad_dates_and_reversed_dates_give_an_error_not_a_crash(academy_world):
    for args in ({"date_from": "next week", "date_to": "2026-11-09"}, {"date_from": "2026-11-09", "date_to": "2026-11-04"}):
        result = json.loads(find_demo_slots.invoke(args))
        assert "error" in result and "free_slots" not in result


def test_the_search_window_is_capped(academy_world):
    result = json.loads(find_demo_slots.invoke({"date_from": "2026-11-04", "date_to": "2030-01-01"}))
    assert result["checked_to"] == "2026-12-05"


def test_slot_results_carry_a_source_and_the_demo_facts(academy_world):
    result = json.loads(find_demo_slots.invoke({"date_from": "2026-11-04", "date_to": "2026-11-04"}))
    assert result["source"] == "fake calendar (tests)"
    assert (result["timezone"], result["demo_minutes"]) == ("Asia/Kolkata", 45)


def test_a_calendar_error_comes_back_as_an_error_result(academy_world):
    class Broken(FakeCalendar):
        def busy(self, start, end):
            raise CalendarError("Google Calendar answered with HTTP 503.")
    with use_calendar(Broken()):
        result = json.loads(find_demo_slots.invoke({"date_from": "2026-11-04", "date_to": "2026-11-09"}))
    assert result["error"] == "Google Calendar answered with HTTP 503." and "free_slots" not in result


# ---- create_demo_event ---------------------------------------------------------------------------

def test_booking_creates_one_event_with_the_first_name_only(academy_world):
    result = book(child="Arjun Kumar Reddy")
    assert result["booked"] is True and result["child"] == "Arjun"
    assert (result["start"], result["end"]) == ("2026-11-07T10:00", "2026-11-07T10:45")
    (event,) = academy_world.calendar.events.values()
    assert event["summary"] == "Demo class - Arjun"
    assert event["parent"] == parent_key(PARENT) and PARENT not in str(event)  # the email itself is not stored


def test_times_are_read_as_india_time_and_other_offsets_are_converted(academy_world):
    book(start="04:30", end="05:15")  # 04:30 India time on a Saturday: not an academy slot, but the owner may book it
    other = json.loads(create_demo_event.invoke({"parent_email": "b@example.invalid", "child_first_name": "Ria",
                                                 "start": "2026-11-08T05:00:00+00:00", "end": "2026-11-08T05:45:00+00:00"}))
    assert other["start"] == "2026-11-08T10:30"  # 05:00 UTC is 10:30 in India


BAD_BOOKINGS = {
    "no @ in email": dict(parent_email="parent.example.invalid", child_first_name="Arjun", start="2026-11-07T10:00", end="2026-11-07T10:45"),
    "no child name": dict(parent_email=PARENT, child_first_name="  ", start="2026-11-07T10:00", end="2026-11-07T10:45"),
    "not a time": dict(parent_email=PARENT, child_first_name="Arjun", start="Saturday morning", end="2026-11-07T10:45"),
    "date only": dict(parent_email=PARENT, child_first_name="Arjun", start="2026-11-07", end="2026-11-07"),
    "in the past": dict(parent_email=PARENT, child_first_name="Arjun", start="2026-11-03T10:00", end="2026-11-03T10:45"),
    "ends before it starts": dict(parent_email=PARENT, child_first_name="Arjun", start="2026-11-07T10:45", end="2026-11-07T10:00"),
    "far too long": dict(parent_email=PARENT, child_first_name="Arjun", start="2026-11-07T10:00", end="2026-11-07T18:00"),
}


@pytest.mark.parametrize("case", BAD_BOOKINGS)
def test_bad_booking_input_is_refused_and_nothing_is_written(academy_world, case):
    result = json.loads(create_demo_event.invoke(BAD_BOOKINGS[case]))
    assert result["booked"] is False and result["error"]
    assert academy_world.calendar.events == {}
    assert "create_demo" not in academy_world.calendar.calls


def test_double_booking_a_time_is_refused(academy_world):
    assert book()["booked"] is True
    second = book(email="b@example.invalid", child="Ria", start="10:30", end="11:15")
    assert second["booked"] is False and "taken" in second["error"]
    assert len(academy_world.calendar.events) == 1


def test_a_child_with_a_demo_already_booked_is_sent_to_reschedule(academy_world):
    book()
    again = book(day="2026-11-09", start="17:00", end="17:45", email=PARENT.upper())  # same parent, e-mail in capitals
    assert again["booked"] is False and "reschedule_demo" in again["error"]


def test_a_calendar_error_while_booking_is_reported_not_raised(academy_world):
    class Broken(FakeCalendar):
        def create_demo(self, *args, **kwargs):
            raise CalendarError("Google Calendar answered with HTTP 403.")
    with use_calendar(Broken()):
        result = book()
    assert result["booked"] is False and "403" in result["error"]


# ---- reschedule_demo -----------------------------------------------------------------------------

def move(new_day="2026-11-09", start="17:00", end="17:45", email=PARENT, child="Arjun") -> dict:
    return json.loads(reschedule_demo.invoke({"parent_email": email, "child_first_name": child,
                                              "new_start": f"{new_day}T{start}", "new_end": f"{new_day}T{end}"}))


def test_rescheduling_moves_the_same_event(academy_world):
    event_id = book()["event_id"]
    result = move()
    assert result["rescheduled"] is True and result["event_id"] == event_id
    assert (result["old_start"], result["start"]) == ("2026-11-07T10:00", "2026-11-09T17:00")
    assert len(academy_world.calendar.events) == 1
    assert "2026-11-07T10:00" in slots(academy_world) and "2026-11-09T17:00" not in slots(academy_world)


def test_an_event_can_move_to_a_time_that_overlaps_its_own_old_time(academy_world):
    book()
    assert move(new_day="2026-11-07", start="10:15", end="11:00")["rescheduled"] is True


def test_rescheduling_into_someone_elses_time_is_refused(academy_world):
    book()
    book(day="2026-11-09", start="17:00", end="17:45", email="b@example.invalid", child="Ria")
    result = move()
    assert result["rescheduled"] is False and "taken" in result["error"]
    assert [e["start"] for e in academy_world.calendar.events.values() if e["child"] == "arjun"] == [at("2026-11-07", "10:00")]


def test_reschedule_and_cancel_only_touch_the_right_childs_event(academy_world):
    book()
    book(day="2026-11-09", start="18:00", end="18:45", email="b@example.invalid", child="Ria")
    move(new_day="2026-11-09", start="17:00", end="17:45")
    assert sorted(e["child"] for e in academy_world.calendar.events.values()) == ["arjun", "ria"]
    ria = next(e for e in academy_world.calendar.events.values() if e["child"] == "ria")
    assert ria["start"] == at("2026-11-09", "18:00")


def test_rescheduling_with_nothing_booked_or_two_booked_is_an_error(academy_world):
    assert move()["rescheduled"] is False
    for day in ("2026-11-07", "2026-11-09"):  # two events for the same parent and child, made behind the tool's back
        academy_world.calendar.create_demo("Demo class - Arjun", at(day, "10:00"), at(day, "10:45"), PARENT, "Arjun")
    result = move(new_day="2026-11-14")
    assert result["rescheduled"] is False and "Question" in result["error"]


# ---- cancel_demo ---------------------------------------------------------------------------------

def test_cancelling_removes_the_event_and_frees_the_slot(academy_world):
    book()
    result = json.loads(cancel_demo.invoke({"parent_email": PARENT, "child_first_name": "Arjun"}))
    assert result["cancelled"] is True and result["was_start"] == "2026-11-07T10:00"
    assert academy_world.calendar.events == {}
    assert "2026-11-07T10:00" in slots(academy_world)


def test_cancelling_when_nothing_is_booked_is_an_error_and_deletes_nothing(academy_world):
    book(email="b@example.invalid")
    result = json.loads(cancel_demo.invoke({"parent_email": PARENT, "child_first_name": "Arjun"}))
    assert result["cancelled"] is False
    assert len(academy_world.calendar.events) == 1 and "delete_demo" not in academy_world.calendar.calls


def test_every_result_names_its_source(academy_world):
    results = [book(), move(), json.loads(cancel_demo.invoke({"parent_email": PARENT, "child_first_name": "Arjun"}))]
    assert all(r["source"] == "fake calendar (tests)" for r in results)
