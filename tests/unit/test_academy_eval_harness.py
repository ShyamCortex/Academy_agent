"""The eval harness: a scripted owner runs the real academy graph and the run is recorded as a Trace."""

import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from academy_helpers import triage_says
from email_assistant.academy import dates
from email_assistant.academy.calendar_backend import FakeCalendar
from email_assistant.academy.dataset import Message
from email_assistant.academy.eval_harness import QUESTION_ANSWER, UsageTracker, next_slot_start, run_message, scripted_owner, seed_calendar
from email_assistant.academy.graph import build_graph
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message

SENDER = "Parent A <parent.a@example.invalid>"


def message(label="book_demo", **kwargs) -> Message:
    base = dict(id="T-1", split="dev", language="en", text="Please book a demo for Arjun.", expected_label=label,
                expected_tool_calls=[], must_include=[], must_not_say=[], sender=SENDER)
    return Message(**{**base, **kwargs})


SLOT = "2026-11-07T10:00"  # a Saturday slot; the fixed test clock is Wednesday 2026-11-04 10:00


def booking_script():
    return ScriptedModel(
        tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-10"),
        tool_call_message("create_demo_event", parent_email="parent.a@example.invalid", child_first_name="Arjun", start=SLOT, end="2026-11-07T10:45"),
        tool_call_message("send_reply", to="parent.a@example.invalid", subject="Demo", body="Booked for 7 Nov at 10:00. It is free (₹0)."),
        tool_call_message("Done", done=True),
    )


def test_a_booking_run_is_recorded_with_its_calls_outputs_and_the_owners_stops(academy_world):
    with use_fake_models(triage=triage_says("book_demo"), agent=booking_script()):
        trace = run_message(build_graph(), message("book_demo"))
    assert trace.label == "book_demo" and trace.finished
    assert [c.name for c in trace.calls] == ["find_demo_slots", "create_demo_event", "send_reply", "done"]
    assert trace.asked == ["create_demo_event", "send_reply"]            # the scripted owner was asked about both writes
    assert [name for name, _ in trace.outputs] == ["find_demo_slots", "create_demo_event", "send_reply"]
    assert SLOT in json.loads(trace.outputs[0][1])["free_slots"] and json.loads(trace.outputs[1][1])["booked"] is True
    assert trace.drafts()[0]["to"] == "parent.a@example.invalid"
    assert trace.seconds >= 0


def test_the_real_calendar_and_outbox_are_not_touched(academy_world):
    with use_fake_models(triage=triage_says("book_demo"), agent=booking_script()):
        run_message(build_graph(), message("book_demo"))
    assert academy_world.calendar.events == {}       # the harness used its own throwaway calendar
    assert academy_world.outbox.exists()             # the reply went to the outbox path from the environment (a temp file here)


def test_each_message_gets_a_fresh_calendar(academy_world):
    graph = build_graph()
    for _ in range(2):   # the same slot is booked twice: it would be "taken" if the calendar were shared
        with use_fake_models(triage=triage_says("book_demo"), agent=booking_script()):
            trace = run_message(graph, message("book_demo"))
        assert json.loads(trace.outputs[1][1])["booked"] is True


def test_a_sensitive_message_stops_at_the_owner_and_nothing_is_called(academy_world):
    agent = ScriptedModel()   # any call to the drafter would raise: it has no script
    with use_fake_models(triage=triage_says("sensitive"), agent=agent):
        trace = run_message(build_graph(), message("sensitive"))
    assert trace.label == "sensitive" and trace.calls == [] and trace.outputs == [] and trace.finished
    assert trace.asked == ["Sensitive message: needs you personally"] and agent.calls == []


def test_an_ignored_message_ends_without_stops(academy_world):
    with use_fake_models(triage=triage_says("ignore"), agent=ScriptedModel()):
        trace = run_message(build_graph(), message("ignore"))
    assert trace.label == "ignore" and trace.asked == [] and trace.calls == []


def test_the_scripted_owners_answer_to_a_question_is_not_evidence(academy_world):
    agent = ScriptedModel(tool_call_message("Question", content="What is the fee?"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        trace = run_message(build_graph(), message("fees_plans"))
    assert [c.name for c in trace.calls] == ["question", "done"] and trace.asked == ["Question"]
    assert trace.outputs == []                                    # the canned answer would otherwise be "a tool output"
    received = [m["content"] if isinstance(m, dict) else m.content for m in agent.calls[1]]
    assert any(QUESTION_ANSWER in str(c) for c in received)       # but the model did receive it


def test_an_agent_that_keeps_asking_is_cut_off_and_marked_unfinished(academy_world):
    asks = [tool_call_message("send_reply", to="parent.a@example.invalid", subject="s", body=f"b{i}") for i in range(6)]
    with use_fake_models(triage=triage_says("fees_plans"), agent=ScriptedModel(*asks)):
        trace = run_message(build_graph(), message("fees_plans"), max_requests=3)
    assert trace.finished is False and len(trace.asked) == 3


def test_the_scripted_owner_answers():
    ask = lambda action, accept: {"action_request": {"action": action}, "config": {"allow_accept": accept}}  # noqa: E731
    assert scripted_owner(ask("send_reply", True)) == {"type": "accept"}
    assert scripted_owner(ask("Question", False))["type"] == "response"
    assert scripted_owner(ask("Sensitive message: needs you personally", False)) == {"type": "ignore"}


# ---- token counting --------------------------------------------------------------------------------

class ChatFake:
    """Stands in for a real chat model: its calls go through LangChain, so callbacks and token counts happen."""

    def __init__(self, *answers: AIMessage):
        self.inner = GenericFakeChatModel(messages=iter(answers))

    def bind_tools(self, tools, tool_choice=None):
        return self.inner

    def with_structured_output(self, schema):
        def call(value, config):
            self.inner.invoke(value, config)
            return schema(reasoning="r", classification="sensitive")
        return RunnableLambda(call)


def with_usage(message: AIMessage, tokens_in: int, tokens_out: int) -> AIMessage:
    message.usage_metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}
    return message


def test_tokens_are_counted_per_role_from_the_wrapper_names(academy_world):
    agent = ChatFake(
        with_usage(AIMessage(content="", tool_calls=[{"name": "get_plans", "args": {}, "id": "c1"}]), 300, 20),
        with_usage(AIMessage(content="", tool_calls=[{"name": "Done", "args": {"done": True}, "id": "c2"}]), 400, 5),
    )

    class Routing(ChatFake):
        def with_structured_output(self, schema):
            def call(value, config):
                self.inner.invoke(value, config)
                return schema(reasoning="r", classification="fees_plans")
            return RunnableLambda(call)

    with use_fake_models(triage=Routing(with_usage(AIMessage(content="x"), 100, 10)), agent=agent):
        trace = run_message(build_graph(), message("fees_plans"))
    assert trace.usage == {"triage": {"calls": 1, "input_tokens": 100, "output_tokens": 10},
                           "agent": {"calls": 2, "input_tokens": 700, "output_tokens": 25}}
    assert [c.name for c in trace.calls] == ["get_plans", "done"]


def test_a_tracker_with_no_model_calls_is_empty():
    assert UsageTracker().snapshot() == {}


# ---- the calendar: fake and seeded by default, real only on request ---------------------------------

DEMO = {"child_first_name": "Arjun", "weekday": "Saturday", "time": "10:00"}


def reschedule_row(**kw) -> Message:
    return message("reschedule_cancel", id="R-1", existing_demo=DEMO, **kw)


def test_a_message_with_an_existing_demo_gets_that_booking_in_a_fresh_fake_calendar(academy_world):
    calendar = seed_calendar(reschedule_row())
    assert isinstance(calendar, FakeCalendar) and calendar.calls == []     # the seeding is not the agent's doing
    (found,) = calendar.find_demos("parent.a@example.invalid", "Arjun", academy_world.now)
    assert dates.iso_minutes(found.start) == "2026-11-07T10:00" and dates.iso_minutes(found.end) == "2026-11-07T10:45"
    assert calendar.find_demos("someone.else@example.invalid", "Arjun", academy_world.now) == []      # only for the sender
    assert calendar.find_demos("parent.a@example.invalid", "Ria", academy_world.now) == []            # only for that child


def test_a_message_without_one_gets_an_empty_calendar(academy_world):
    assert seed_calendar(message()).events == {}


def test_the_seed_is_the_same_every_time_and_the_calendars_are_separate(academy_world):
    first, second = seed_calendar(reschedule_row()), seed_calendar(reschedule_row())
    assert first is not second and list(first.events.values()) == list(second.events.values())
    first.events.clear()
    assert len(second.events) == 1


@pytest.mark.parametrize("now,weekday,hhmm,expected", [
    ("2026-11-04T10:00", "Saturday", "10:00", "2026-11-07T10:00"),
    ("2026-11-04T10:00", "Wednesday", "17:00", "2026-11-04T17:00"),     # today, still ahead
    ("2026-11-04T18:00", "Wednesday", "17:00", "2026-11-11T17:00"),     # today, already gone: next week
    ("2026-11-07T10:00", "Saturday", "10:00", "2026-11-14T10:00"),      # exactly now is not in the future
    ("2026-11-08T09:00", "Monday", "18:00", "2026-11-09T18:00"),
])
def test_the_booking_is_the_next_future_occurrence_of_that_weekday_and_time(monkeypatch, now, weekday, hhmm, expected):
    monkeypatch.setattr(dates, "now_ist", lambda: dates.parse_local_datetime(now))
    assert dates.iso_minutes(next_slot_start(weekday, hhmm)) == expected


def run_script(row: Message, *calls, label="reschedule_cancel", **kw):
    agent = ScriptedModel(*calls, tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says(label), agent=agent):
        return run_message(build_graph(), row, **kw)


def test_a_reschedule_row_can_actually_be_rescheduled(academy_world):
    trace = run_script(reschedule_row(),
                       tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-10"),
                       tool_call_message("reschedule_demo", parent_email="parent.a@example.invalid", child_first_name="Arjun",
                                         new_start="2026-11-09T17:00", new_end="2026-11-09T17:45"))
    moved = json.loads(trace.outputs[-1][1])
    assert moved["rescheduled"] is True and moved["old_start"] == "2026-11-07T10:00" and moved["start"] == "2026-11-09T17:00"
    assert "2026-11-07T10:00" not in json.loads(trace.outputs[0][1])["free_slots"]       # the booked slot is not offered to anyone


def test_a_cancel_row_can_actually_be_cancelled_and_without_the_seed_it_could_not(academy_world):
    cancel = tool_call_message("cancel_demo", parent_email="parent.a@example.invalid", child_first_name="Arjun")
    assert json.loads(run_script(reschedule_row(), cancel).outputs[-1][1])["cancelled"] is True
    plain = message("reschedule_cancel", id="R-2")                                       # same message, no existing_demo
    cancel_again = tool_call_message("cancel_demo", parent_email="parent.a@example.invalid", child_first_name="Arjun")
    result = json.loads(run_script(plain, cancel_again).outputs[-1][1])
    assert result["cancelled"] is False and "No upcoming demo" in result["error"]


def test_the_seeded_booking_never_reaches_the_calendar_the_settings_select(academy_world):
    run_script(reschedule_row(), tool_call_message("cancel_demo", parent_email="parent.a@example.invalid", child_first_name="Arjun"))
    assert academy_world.calendar.events == {} and academy_world.calendar.calls == []     # not touched at all


def test_the_real_calendar_is_used_only_when_asked_and_nothing_is_seeded(academy_world):
    """In the unit tests the 'real' calendar is academy_world.calendar, a stand-in installed by conftest."""
    book = lambda: [  # noqa: E731
        tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-10"),
        tool_call_message("create_demo_event", parent_email="parent.a@example.invalid", child_first_name="Arjun", start=SLOT, end="2026-11-07T10:45")]
    run_script(message("book_demo"), *book(), label="book_demo")
    assert academy_world.calendar.events == {}                                           # default: fake, stand-in untouched
    run_script(reschedule_row(), *book(), label="reschedule_cancel", real_calendar=True)
    assert len(academy_world.calendar.events) == 1                                       # the booking went to the selected calendar
    (event,) = academy_world.calendar.events.values()
    assert event["start"] == dates.parse_local_datetime(SLOT)                            # only the agent's booking: the seed was skipped
