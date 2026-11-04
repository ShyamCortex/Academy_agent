"""The eval harness: a scripted owner runs the real academy graph and the run is recorded as a Trace."""

import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from academy_helpers import triage_says
from email_assistant.academy.dataset import Message
from email_assistant.academy.eval_harness import QUESTION_ANSWER, UsageTracker, run_message, scripted_owner
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
