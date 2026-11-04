"""Hard rule 1: deny by default. Only tools on the read-only allow-list run without the owner."""

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from academy_helpers import Run, triage_says
from email_assistant.academy.graph import build_graph
from email_assistant.academy.guards import READ_ONLY_TOOLS, needs_approval
from email_assistant.academy.tools import ACADEMY_TOOLS
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message


def _drafting(*script):
    return ScriptedModel(*script)


def test_the_allow_list_is_exactly_the_six_lookups():
    assert READ_ONLY_TOOLS == {"find_demo_slots", "get_plans", "get_batch_timings", "get_workshops", "get_tournaments", "search_knowledge"}
    assert {t.name for t in ACADEMY_TOOLS} >= READ_ONLY_TOOLS  # and every one is a real tool


@pytest.mark.parametrize("name", ["create_demo_event", "reschedule_demo", "cancel_demo", "send_reply", "Question", "anything_new",
                                  "get_fee_plans", "get_batches", "FIND_DEMO_SLOTS", "find_demo_slots ", ""])
def test_everything_off_the_list_needs_approval(name):
    assert needs_approval(name)


def test_a_read_only_tool_runs_without_asking():
    agent = _drafting(tool_call_message("get_plans"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        result = run.start()
    assert "__interrupt__" not in result
    tool_message = next(m for m in result["messages"] if m.type == "tool")
    assert json.loads(tool_message.content)["plans"]


@pytest.mark.parametrize("tool_name,args", [
    ("send_reply", {"to": "p@example.invalid", "subject": "Re", "body": "Hello"}),
    ("create_demo_event", {"parent_email": "p@example.invalid", "child_first_name": "Arjun",
                           "start": "2026-11-07T14:00", "end": "2026-11-07T14:45"}),
    ("reschedule_demo", {"parent_email": "p@example.invalid", "child_first_name": "Arjun",
                         "new_start": "2026-11-09T17:00", "new_end": "2026-11-09T17:45"}),
    ("cancel_demo", {"parent_email": "p@example.invalid", "child_first_name": "Arjun"}),
])
def test_academy_write_tools_wait_for_the_owner(tool_name, args):
    agent = _drafting(tool_call_message(tool_name, **args), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        result = run.start()
        request = run.pending(result)
        assert request["action_request"]["action"] == tool_name
        assert run.drafter_messages()[-1].tool_calls  # the draft is waiting...
        assert not [m for m in run.drafter_messages() if m.type == "tool"]  # ...and nothing ran yet
        run.answer(type="accept")
    assert any(m.type == "tool" and "source" in m.content for m in run.state.values["messages"])


def _made_up_write_tool(calls):
    @tool
    def refund_fees(student: str, amount: str) -> str:
        """Refund money to a parent."""
        calls.append((student, amount))
        return "refunded"
    return refund_fees


def test_a_new_write_tool_cannot_skip_approval():
    """A tool nobody put on any list still stops and asks."""
    calls = []
    graph = build_graph(tools=[*ACADEMY_TOOLS, _made_up_write_tool(calls)])
    agent = _drafting(tool_call_message("refund_fees", student="Arjun", amount="500"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run(graph=graph)
        result = run.start()
        assert run.pending(result)["action_request"]["action"] == "refund_fees"
        assert calls == []  # not run
        run.answer(type="accept")
    assert calls == [("Arjun", "500")]  # run once, after approval


@pytest.mark.parametrize("answer", [{"type": "ignore"}, {"type": "response", "args": "no thanks"}])
def test_a_declined_new_write_tool_never_runs(answer):
    calls = []
    graph = build_graph(tools=[*ACADEMY_TOOLS, _made_up_write_tool(calls)])
    agent = _drafting(tool_call_message("refund_fees", student="Arjun", amount="500"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run(graph=graph)
        run.start()
        run.answer(**answer)
    assert calls == []


def test_an_edited_new_write_tool_runs_once_with_the_edited_arguments():
    calls = []
    graph = build_graph(tools=[*ACADEMY_TOOLS, _made_up_write_tool(calls)])
    agent = _drafting(tool_call_message("refund_fees", student="Arjun", amount="500"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run(graph=graph)
        run.start()
        run.answer(type="edit", args={"args": {"student": "Arjun", "amount": "100"}})
    assert calls == [("Arjun", "100")]


def test_a_tool_that_does_not_exist_is_never_run():
    agent = _drafting(tool_call_message("wipe_database"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("schedule"), agent=agent):
        result = Run().start()
    assert "__interrupt__" not in result
    tool_message = next(m for m in result["messages"] if m.type == "tool")
    assert "Unknown tool" in tool_message.content


def test_two_tool_calls_in_one_turn_are_cut_to_one():
    """Resuming re-runs the tool step, so two approved writes in one turn could be sent twice."""
    two = AIMessage(content="", tool_calls=[
        {"name": "send_reply", "args": {"to": "a@example.invalid", "subject": "S", "body": "B"}, "id": "c1"},
        {"name": "create_demo_event", "args": {"parent_email": "a@example.invalid", "child_first_name": "A",
                                               "start": "2026-11-07T14:00", "end": "2026-11-07T14:45"}, "id": "c2"},
    ])
    agent = _drafting(two, tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        result = run.start()
        assert run.pending(result)["action_request"]["action"] == "send_reply"
        assert len(run.drafter_messages()[-1].tool_calls) == 1
        run.answer(type="accept")
    assert not any("booked" in str(m.content) for m in run.state.values["messages"])


def test_done_ends_the_run_without_running_other_calls():
    done_and_write = AIMessage(content="", tool_calls=[
        {"name": "Done", "args": {"done": True}, "id": "c1"},
        {"name": "send_reply", "args": {"to": "a", "subject": "s", "body": "b"}, "id": "c2"},
    ])
    with use_fake_models(triage=triage_says("schedule"), agent=_drafting(done_and_write)):
        result = Run().start()
    assert "__interrupt__" not in result
    assert not any(m.type == "tool" for m in result["messages"])
