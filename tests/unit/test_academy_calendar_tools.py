"""Calendar tools on a fake calendar: slot finder, booking, rescheduling, cancelling. No keys, no network."""

import json
from datetime import datetime, timedelta

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


def search(date_from="2026-11-04", date_to="2026-11-09", **more) -> dict:
    return json.loads(find_demo_slots.invoke({"date_from": date_from, "date_to": date_to, **more}))


def slots(world, date_from="2026-11-04", date_to="2026-11-09", **more) -> list[str]:
    """The start of every free slot, like '2026-11-07T10:00'."""
    return [s["start"] for s in search(date_from, date_to, **more)["free_slots"]]


def book(day="2026-11-07", start="10:00", end="10:45", email=PARENT, child="Arjun") -> dict:
    return json.loads(create_demo_event.invoke(
        {"parent_email": email, "child_first_name": child, "start": f"{day}T{start}", "end": f"{day}T{end}"}))


# ---- find_demo_slots -----------------------------------------------------------------------------

def test_slots_follow_the_academy_hours_on_an_empty_calendar(academy_world):
    assert slots(academy_world, date_to="2026-11-09")[:5] == [
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


# ---- find_demo_slots: labels, look-ahead, weekday filter (v1 eval: the model wrote weekdays and dates itself, wrongly) ----

WEEKDAYS_NEEDED = sorted(config.DEMO_SLOT_TIMES)


def test_every_slot_has_a_label_with_its_weekday_date_and_24_hour_time(academy_world):
    result = search("2026-11-04", "2026-11-09")
    by_start = {s["start"]: s for s in result["free_slots"]}
    assert by_start["2026-11-09T18:00"] == {"label": "Monday 2026-11-09 18:00 IST", "start": "2026-11-09T18:00", "end": "2026-11-09T18:45"}
    assert by_start["2026-11-07T10:00"]["label"] == "Saturday 2026-11-07 10:00 IST"
    for slot in result["free_slots"]:   # the label always agrees with the real calendar
        start = at(*slot["start"].split("T"))
        assert slot["label"] == f"{start:%A %Y-%m-%d %H:%M} IST"
        assert slot["end"] == dates.iso_minutes(start + timedelta(minutes=config.DEMO_CLASS_MINUTES))


def test_the_answer_carries_the_demo_length_and_fee_from_config(academy_world):
    result = search()
    assert (result["demo_minutes"], result["demo_fee_rupees"]) == (config.DEMO_CLASS_MINUTES, config.DEMO_CLASS_FEE_RUPEES)
    assert result["demo_terms"] == "free, 45 minutes"
    assert result["demo_days"] == ["Monday", "Wednesday", "Saturday"]


def test_a_paid_demo_is_described_with_its_fee(academy_world, monkeypatch):
    monkeypatch.setattr(config, "DEMO_CLASS_FEE_RUPEES", 300)
    assert search()["demo_terms"] == "₹300, 45 minutes"


def test_a_one_day_search_still_covers_the_next_three_of_every_demo_weekday(academy_world):
    result = search("2026-11-04", "2026-11-04")
    days = {}
    for slot in result["free_slots"]:
        days.setdefault(slot["label"].split()[0], set()).add(slot["start"][:10])
    assert {name: sorted(found) for name, found in days.items()} == {
        "Wednesday": ["2026-11-04", "2026-11-11", "2026-11-18"],
        "Saturday": ["2026-11-07", "2026-11-14", "2026-11-21"],
        "Monday": ["2026-11-09", "2026-11-16", "2026-11-23"],
    }
    assert result["checked_to"] == "2026-11-23"


def test_the_lookahead_does_not_count_a_day_whose_slots_have_all_passed(academy_world, monkeypatch):
    monkeypatch.setattr(dates, "now_ist", lambda: at("2026-11-04", "17:30"))   # today's only Wednesday slot (17:00) is over
    result = search("2026-11-04", "2026-11-04")
    wednesdays = sorted({s["start"][:10] for s in result["free_slots"] if s["label"].startswith("Wednesday")})
    assert wednesdays == ["2026-11-11", "2026-11-18", "2026-11-25"]


def test_a_search_the_model_makes_longer_than_the_lookahead_is_kept(academy_world):
    assert search("2026-11-04", "2026-12-02")["checked_to"] == "2026-12-02"


def test_the_lookahead_starts_from_the_requested_date(academy_world):
    result = search("2026-12-01", "2026-12-01")      # a Tuesday
    firsts = {}
    for slot in result["free_slots"]:
        firsts.setdefault(slot["label"].split()[0], slot["start"][:10])
    assert firsts == {"Wednesday": "2026-12-02", "Saturday": "2026-12-05", "Monday": "2026-12-07"}


def test_a_date_in_the_past_is_searched_from_today(academy_world):
    result = search("2026-10-01", "2026-10-05")
    assert result["checked_from"] == "2026-11-04" and result["free_slots"]


def test_the_weekday_filter_keeps_only_that_day_and_still_covers_three_of_them(academy_world):
    result = search("2026-11-04", "2026-11-04", weekday="Monday")
    assert [s["label"] for s in result["free_slots"]] == [
        "Monday 2026-11-09 17:00 IST", "Monday 2026-11-09 18:00 IST", "Monday 2026-11-16 17:00 IST",
        "Monday 2026-11-16 18:00 IST", "Monday 2026-11-23 17:00 IST", "Monday 2026-11-23 18:00 IST"]
    assert result["weekday"] == "Monday" and result["checked_to"] == "2026-11-23"


@pytest.mark.parametrize("written", ["monday", "MONDAY", " Mon ", "mon.", "Monday "])
def test_the_weekday_filter_accepts_common_spellings(academy_world, written):
    assert {s["start"][:10] for s in search(weekday=written)["free_slots"]} == {"2026-11-09", "2026-11-16", "2026-11-23"}


def test_an_empty_weekday_means_all_days(academy_world):
    assert slots(academy_world, weekday="") == slots(academy_world)


def test_a_weekday_with_no_demo_gives_no_slots_and_says_which_days_have_them(academy_world):
    result = search(weekday="Tuesday")
    assert result["free_slots"] == [] and "No demo classes are held on Tuesday" in result["note"] and "error" not in result
    assert "Monday, Wednesday, Saturday" in result["note"]


@pytest.mark.parametrize("bad", ["someday", "Mo", "7", "next"])
def test_a_weekday_that_is_not_a_weekday_is_an_error(academy_world, bad):
    result = search(weekday=bad)
    assert "weekday" in result["error"] and "free_slots" not in result


def test_busy_slots_are_left_out_of_the_extended_window_too(academy_world):
    academy_world.calendar._fixed = [Block(at("2026-11-16", "17:00"), at("2026-11-16", "17:45"))]
    labels = [s["label"] for s in search(weekday="Monday")["free_slots"]]
    assert "Monday 2026-11-16 17:00 IST" not in labels and "Monday 2026-11-16 18:00 IST" in labels


def test_the_label_is_the_only_place_a_weekday_is_written_and_start_and_end_are_what_booking_takes(academy_world):
    """The model copies label for the parent and start/end for the booking; neither needs date arithmetic."""
    slot = next(s for s in search(weekday="Monday")["free_slots"] if s["label"].endswith("18:00 IST"))
    result = json.loads(create_demo_event.invoke({"parent_email": PARENT, "child_first_name": "Arjun", "start": slot["start"], "end": slot["end"]}))
    assert result["booked"] is True and result["start"] == "2026-11-09T18:00"


def test_the_parameter_list_of_find_demo_slots_gained_only_an_optional_weekday():
    assert find_demo_slots.args["weekday"]["default"] == "" and find_demo_slots.args_schema.model_json_schema()["required"] == ["date_from", "date_to"]


# ---- create_demo_event ---------------------------------------------------------------------------

def test_booking_creates_one_event_with_the_first_name_only(academy_world):
    result = book(child="Arjun Kumar Reddy")
    assert result["booked"] is True and result["child"] == "Arjun"
    assert (result["start"], result["end"]) == ("2026-11-07T10:00", "2026-11-07T10:45")
    (event,) = academy_world.calendar.events.values()
    assert event["summary"] == "Demo class - Arjun"
    assert event["parent"] == parent_key(PARENT) and PARENT not in str(event)  # the email itself is not stored


def test_times_are_read_as_india_time_and_other_offsets_are_converted(academy_world):
    other = json.loads(create_demo_event.invoke({"parent_email": "b@example.invalid", "child_first_name": "Ria",
                                                 "start": "2026-11-09T11:30:00+00:00", "end": "2026-11-09T12:15:00+00:00"}))
    assert other["booked"] is True and other["start"] == "2026-11-09T17:00"  # 11:30 UTC is Monday 17:00 in India, a demo slot


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


def test_double_booking_a_slot_is_refused(academy_world):
    assert book()["booked"] is True
    second = book(email="b@example.invalid", child="Ria")            # the same slot, another family
    assert second["booked"] is False and "taken" in second["error"]
    assert len(academy_world.calendar.events) == 1
    assert book(email="b@example.invalid", child="Ria", start="11:30", end="12:15")["booked"] is True  # the next slot is still free


def test_a_slot_blocked_in_the_calendar_cannot_be_booked(academy_world):
    academy_world.calendar._fixed = [Block(at("2026-11-07", "10:30"), at("2026-11-07", "11:00"))]
    result = book()
    assert result["booked"] is False and "taken" in result["error"] and academy_world.calendar.events == {}


# ---- only demo slots can be booked (the rule find_demo_slots follows, enforced in the tool) ------

NOT_SLOTS = {
    "a time between slots": ("2026-11-07", "10:30", "11:15"),
    "a slot time on the wrong weekday": ("2026-11-07", "17:00", "17:45"),      # Monday's time, on a Saturday
    "a weekday with no demo at all": ("2026-11-10", "17:00", "17:45"),         # Tuesday
    "an early hour": ("2026-11-07", "07:00", "07:45"),
    "not on the minute": ("2026-11-07", "10:00:30", "10:45:30"),
}


@pytest.mark.parametrize("case", NOT_SLOTS)
def test_a_start_that_is_not_a_demo_slot_is_refused_with_a_clear_error_and_nothing_is_written(academy_world, case):
    day, start, end = NOT_SLOTS[case]
    result = book(day=day, start=start, end=end)
    assert result["booked"] is False
    assert "not a demo slot" in result["error"] and "find_demo_slots" in result["error"]
    assert academy_world.calendar.events == {} and "create_demo" not in academy_world.calendar.calls


@pytest.mark.parametrize("end", ["10:30", "11:00", "10:46", "10:44"])
def test_a_demo_slot_with_the_wrong_length_is_refused(academy_world, end):
    result = book(start="10:00", end=end)
    assert result["booked"] is False and f"exactly {config.DEMO_CLASS_MINUTES} minutes" in result["error"]
    assert academy_world.calendar.events == {}


def test_every_slot_find_demo_slots_offers_can_be_booked_and_then_stops_being_offered(academy_world):
    offered = slots(academy_world, "2026-11-04", "2026-11-30")
    assert len(offered) > 10
    for number, slot in enumerate(offered):
        start = at(*slot.split("T"))
        end = dates.iso_minutes(start + timedelta(minutes=config.DEMO_CLASS_MINUTES))
        result = json.loads(create_demo_event.invoke({"parent_email": f"family{number}@example.invalid", "child_first_name": "Kid",
                                                      "start": slot, "end": end}))
        assert result["booked"] is True, (slot, result)
    assert slots(academy_world, "2026-11-04", "2026-11-30") == []


def test_the_tool_text_tells_the_model_to_use_a_returned_slot():
    assert "free_slots of find_demo_slots" in create_demo_event.description


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


def test_an_event_does_not_block_its_own_move(academy_world):
    # No two demo slots overlap, so "move onto its own time" is the case left: the event being moved is not "taken".
    book()
    assert move(new_day="2026-11-07", start="10:00", end="10:45")["rescheduled"] is True


@pytest.mark.parametrize("case", NOT_SLOTS)
def test_a_reschedule_to_a_time_that_is_not_a_demo_slot_is_refused_and_nothing_moves(academy_world, case):
    book()
    day, start, end = NOT_SLOTS[case]
    result = move(new_day=day, start=start, end=end)
    assert result["rescheduled"] is False
    assert "not a demo slot" in result["error"] and "find_demo_slots" in result["error"]
    assert [e["start"] for e in academy_world.calendar.events.values()] == [at("2026-11-07", "10:00")]
    assert "move_demo" not in academy_world.calendar.calls


@pytest.mark.parametrize("end", ["17:30", "18:00", "17:46"])
def test_a_reschedule_to_a_demo_slot_with_the_wrong_length_is_refused(academy_world, end):
    book()
    result = move(start="17:00", end=end)
    assert result["rescheduled"] is False and f"exactly {config.DEMO_CLASS_MINUTES} minutes" in result["error"]
    assert [e["start"] for e in academy_world.calendar.events.values()] == [at("2026-11-07", "10:00")]


def test_every_slot_find_demo_slots_offers_is_a_valid_reschedule_target(academy_world):
    book()
    for slot in slots(academy_world, "2026-11-08", "2026-11-30"):
        start = at(*slot.split("T"))
        end = dates.iso_minutes(start + timedelta(minutes=config.DEMO_CLASS_MINUTES))
        result = json.loads(reschedule_demo.invoke({"parent_email": PARENT, "child_first_name": "Arjun", "new_start": slot, "new_end": end}))
        assert result["rescheduled"] is True, (slot, result)


def test_the_reschedule_tool_text_tells_the_model_to_use_a_returned_slot():
    assert "free_slots of find_demo_slots" in reschedule_demo.description


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
    result = move(new_day="2026-11-14", start="10:00", end="10:45")  # a real Saturday slot, so only the "two booked" rule can refuse it
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
