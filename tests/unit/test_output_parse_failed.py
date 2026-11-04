"""Groq 400 `output_parse_failed` (seen once in the v1 dev eval, on a retry it worked) is recovered like `tool_use_failed`.

Safe because the error comes before any tool runs: asking the model again cannot repeat a booking or a reply.
It is asked at most MAX_NUDGES times, nothing from the error is put in the reminder, and the hard rules in code
(approval, sensitive block) do not depend on it.
"""

import groq
import httpx
import pytest
from langchain_core.messages import AIMessage

from academy_helpers import Run, route, triage_says
from email_assistant import models
from email_assistant.academy import e2e
from email_assistant.academy.dataset import load_messages
from email_assistant.academy.outbox import read_outbox
from email_assistant.models import (
    MAX_NUDGES, describe_error, error_code, is_retryable, structured_answer_nudge, structured_llm, tool_call_nudge, tool_llm, use_fake_models,
)
from email_assistant.schemas import RouterSchema
from fakes import ScriptedModel, tool_call_message

PRIVATE = "ZQX-PRIVATE-TEXT"
PARENT = "parent.a@example.invalid"


def groq_error(code="output_parse_failed", message="Failed to parse the model's output", status=400, extra=""):
    """A Groq SDK error built the way the SDK builds it. The text quotes the model's output and the parent's message, on purpose."""
    request = httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions")
    body = {"error": {"message": f"{message} {PRIVATE} {extra}", "type": "invalid_request_error", "code": code, "failed_generation": f"{PRIVATE} half a tool call"}}
    cls = {400: groq.BadRequestError, 429: groq.RateLimitError}[status]
    return cls(f"Error code: {status} - {body}", response=httpx.Response(status, request=request), body=body)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(models, "_sleep", sleeps.append)
    return sleeps


# ---- the pieces ---------------------------------------------------------------------------------------------------

def test_it_is_a_model_mistake_not_something_to_wait_on():
    assert not is_retryable(groq_error())
    # the model's own output can contain words that look like a server problem: still not waited on
    assert not is_retryable(groq_error(extra="service unavailable, rate limit, 503 overloaded"))
    assert is_retryable(groq_error(code="rate_limit_exceeded", status=429, message="slow down"))      # a real rate limit still is


def test_the_agent_gets_a_reminder_that_says_nothing_about_the_error(no_sleep):
    assert tool_call_nudge(groq_error()) == models.NUDGE_UNREADABLE
    assert PRIVATE not in models.NUDGE_UNREADABLE
    assert tool_call_nudge(groq_error(code="invalid_api_key", message="bad key")) is None
    assert tool_call_nudge(groq_error(code="model_not_found", message="no such model")) is None


def test_structured_output_gets_the_same_recovery():
    nudge = structured_answer_nudge("RouterSchema")
    assert nudge(groq_error()) and "RouterSchema" in nudge(groq_error()) and PRIVATE not in nudge(groq_error())
    assert nudge(groq_error(code="invalid_api_key", message="bad key")) is None


def test_the_error_is_logged_by_type_and_code_only():
    assert error_code(groq_error()) == "output_parse_failed"
    described = describe_error(groq_error())
    assert described == "BadRequestError, code output_parse_failed" and PRIVATE not in described


# ---- the calls ------------------------------------------------------------------------------------------------------

def test_a_forced_tool_call_that_fails_to_parse_is_asked_again_with_a_reminder(no_sleep):
    fake = ScriptedModel(groq_error(), tool_call_message("Done", done=True))
    with use_fake_models(agent=fake):
        reply = tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert reply.tool_calls[0]["name"] == "Done" and len(fake.calls) == 2
    assert fake.calls[0] == [{"role": "user", "content": "go"}]                       # the first try was not changed
    assert fake.calls[1][-1] == {"role": "user", "content": models.NUDGE_UNREADABLE}
    assert no_sleep == []                                                              # not waited on


def test_it_gives_up_after_the_limit_and_raises_the_provider_error():
    fake = ScriptedModel(*[groq_error()] * (MAX_NUDGES + 2))
    with use_fake_models(agent=fake), pytest.raises(groq.BadRequestError):
        tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert len(fake.calls) == MAX_NUDGES + 1


def test_a_triage_call_that_fails_to_parse_is_asked_again(no_sleep):
    fake = ScriptedModel(groq_error(), RouterSchema(reasoning="r", classification="respond"))
    with use_fake_models(triage=fake):
        assert structured_llm("triage", RouterSchema).invoke([{"role": "user", "content": "x"}]).classification == "respond"
    assert len(fake.calls) == 2 and PRIVATE not in str(fake.calls[1])


# ---- what recovering must not change ------------------------------------------------------------------------------

def test_a_failure_after_an_approved_booking_does_not_book_twice_or_skip_the_approval(academy_world):
    """The model's answer after the booking is unreadable once. The booking was already made once and is not repeated;
    the reply still waits for the owner."""
    agent = ScriptedModel(
        tool_call_message("create_demo_event", parent_email=PARENT, child_first_name="Arjun", start="2026-11-04T17:00", end="2026-11-04T17:45"),
        groq_error(),                                                                  # the turn after the booking fails to parse
        tool_call_message("send_reply", to=PARENT, subject="Demo", body="Booked for Wednesday 2026-11-04 17:00."),
        tool_call_message("Done", done=True),
    )
    sample = next(m for m in load_messages(e2e.SAMPLES) if m.id == "SAMPLE-04")
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        result = run.start(sample.graph_input())
        assert run.pending(result)["action_request"]["action"] == "create_demo_event" and academy_world.calendar.events == {}
        result = run.answer(type="accept")
        assert run.pending(result)["action_request"]["action"] == "send_reply"          # still asks the owner
        assert len(academy_world.calendar.events) == 1 and not academy_world.outbox.exists()
        result = run.answer(type="accept")
    assert run.pending(result) is None and len(academy_world.calendar.events) == 1
    assert [r["to"] for r in read_outbox()] == [PARENT]
    assert agent.calls and any(c[-1] == {"role": "user", "content": models.NUDGE_UNREADABLE} for c in agent.calls)


def test_a_sensitive_message_stays_with_the_owner_even_if_triage_needs_a_second_try(academy_world):
    triage = ScriptedModel(groq_error(), route("sensitive"))
    agent = ScriptedModel(AIMessage(content="must never be called"))
    with use_fake_models(triage=triage, agent=agent):
        run = Run()
        run.start({"from": f"Parent <{PARENT}>", "text": "I am very angry about my child's treatment."})
    assert agent.calls == [] and academy_world.calendar.events == {} and not academy_world.outbox.exists()
