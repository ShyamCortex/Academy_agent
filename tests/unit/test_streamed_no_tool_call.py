"""Live server (`langgraph dev`): model calls are streamed, so Groq's "Tool choice is required, but model did not call a tool"
arrives as a plain `APIError` with no status code and no `tool_use_failed` code. It crashed the closing `Done` step after the
owner had approved the reply. It is now recognised by error type and message and handled by remind-and-retry.
"""

import json
import uuid

import groq
import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from academy_helpers import triage_says
from email_assistant import models
from email_assistant.academy import e2e
from email_assistant.academy.dataset import load_messages
from email_assistant.academy.graph import build_graph
from email_assistant.academy.outbox import read_outbox
from email_assistant.models import (
    MAX_NUDGES, describe_error, error_code, is_retryable, is_streamed_no_tool_call, structured_answer_nudge, tool_call_nudge, tool_llm, use_fake_models,
)
from fakes import ScriptedModel, tool_call_message

MESSAGE = "Tool choice is required, but model did not call a tool"
PRIVATE = "ZQX-PRIVATE-TEXT"
PARENT = "parent.a@example.invalid"
SAMPLE = next(m for m in load_messages(e2e.SAMPLES) if m.id == "SAMPLE-04")


def streamed_error(message=MESSAGE) -> groq.APIError:
    """The error the Groq SDK raises for an error event inside a stream: a plain APIError, no status, no code (checked against the SDK)."""
    return groq.APIError(message, httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions"), body=None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(models, "_sleep", sleeps.append)
    return sleeps


# ---- recognising it -------------------------------------------------------------------------------------------------

def test_the_streamed_error_has_none_of_the_marks_the_older_recovery_looked_for():
    error = streamed_error()
    assert type(error) is groq.APIError and not hasattr(error, "status_code") and error_code(error) is None
    assert "tool_use_failed" not in str(error)


def test_it_is_recognised_by_type_and_message():
    assert is_streamed_no_tool_call(streamed_error())
    assert is_streamed_no_tool_call(streamed_error("Error: TOOL CHOICE IS REQUIRED, BUT MODEL DID NOT CALL A TOOL."))
    assert tool_call_nudge(streamed_error()) == models.NUDGE_NO_TOOL
    assert structured_answer_nudge("RouterSchema")(streamed_error())


def test_the_openai_sdk_has_the_same_class_name_and_is_recognised_too():
    import openai

    error = openai.APIError(MESSAGE, httpx.Request("POST", "https://api.invalid/x"), body=None)
    assert is_streamed_no_tool_call(error)


def test_an_error_that_caused_it_is_looked_at_too():
    try:
        try:
            raise streamed_error()
        except groq.APIError as inner:
            raise RuntimeError("model step failed") from inner
    except RuntimeError as outer:
        assert is_streamed_no_tool_call(outer) and tool_call_nudge(outer) == models.NUDGE_NO_TOOL


@pytest.mark.parametrize("error", [
    streamed_error("Something else went wrong"),                          # an APIError that is not this one
    streamed_error("Tool choice is required"),                            # only part of the sentence
    RuntimeError(MESSAGE),                                                # the sentence, but not an API error
    ValueError(f"{MESSAGE}"),
    streamed_error("Rate limit reached, please try again"),
])
def test_other_errors_are_not_mistaken_for_it(error):
    assert not is_streamed_no_tool_call(error) and tool_call_nudge(error) is None and structured_answer_nudge("X")(error) is None


def test_it_is_never_waited_on_even_if_its_text_looks_like_a_server_problem():
    assert not is_retryable(streamed_error())
    assert not is_retryable(streamed_error(MESSAGE + ". The service was unavailable, rate limit"))
    assert is_retryable(streamed_error("Service unavailable (503)"))      # an ordinary APIError of that kind still is


def test_it_is_logged_by_type_only():
    assert describe_error(streamed_error()) == "APIError"


# ---- the calls ------------------------------------------------------------------------------------------------------

def test_a_forced_call_that_fails_this_way_is_asked_again_with_a_reminder(no_sleep):
    fake = ScriptedModel(streamed_error(), tool_call_message("Done", done=True))
    with use_fake_models(agent=fake):
        reply = tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert reply.tool_calls[0]["name"] == "Done" and len(fake.calls) == 2
    assert fake.calls[1][-1] == {"role": "user", "content": models.NUDGE_NO_TOOL} and no_sleep == []


def test_it_gives_up_after_the_limit_and_raises_the_provider_error():
    fake = ScriptedModel(*[streamed_error()] * (MAX_NUDGES + 2))
    with use_fake_models(agent=fake), pytest.raises(groq.APIError):
        tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert len(fake.calls) == MAX_NUDGES + 1


# ---- the real streaming path, through the graph ----------------------------------------------------------------------

class StreamedModel(BaseChatModel):
    """A chat model that answers the way a streamed call does: `stream_mode="messages"` makes the graph read it chunk by chunk.

    A scripted exception is raised from inside the stream, after one empty chunk, like an error event in the stream.
    """

    script: list
    streamed: int = 0

    @property
    def _llm_type(self) -> str:
        return "streamed-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.streamed += 1
        item = self.script.pop(0)
        yield ChatGenerationChunk(message=AIMessageChunk(content=""))
        if isinstance(item, BaseException):
            raise item
        chunks = [{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i} for i, c in enumerate(item.tool_calls)]
        yield ChatGenerationChunk(message=AIMessageChunk(content=item.content, tool_call_chunks=chunks))


def drain(graph, value, config):
    """Run the graph the way the live server does (streamed messages) and return the owner's pending request, or None."""
    for _ in graph.stream(value, config, stream_mode="messages"):
        pass
    tasks = graph.get_state(config).tasks
    interrupts = [i for task in tasks for i in task.interrupts]
    return interrupts[0].value[0]["action_request"]["action"] if interrupts else None


def booking_script(closing):
    return [
        tool_call_message("create_demo_event", parent_email=PARENT, child_first_name="Arjun", start="2026-11-04T17:00", end="2026-11-04T17:45"),
        tool_call_message("send_reply", to=PARENT, subject="Demo", body="Booked for Wednesday 2026-11-04 17:00."),
        *closing,
    ]


def run_streamed(agent: StreamedModel):
    graph = build_graph().compile(checkpointer=MemorySaver(), store=InMemoryStore())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        actions = [drain(graph, {"message_input": SAMPLE.graph_input()}, config)]
        while actions[-1]:
            actions.append(drain(graph, Command(resume=[{"type": "accept"}]), config))
    return graph, config, actions[:-1]


def test_the_reported_crash_after_the_approved_reply_is_recovered_and_nothing_is_done_twice(academy_world):
    """The closing Done call fails once with the streamed error. The run still finishes, the event and the reply exist once."""
    agent = StreamedModel(script=booking_script([streamed_error(), tool_call_message("Done", done=True)]))
    graph, config, asked = run_streamed(agent)
    assert asked == ["create_demo_event", "send_reply"]                    # the owner was asked once for each write, and not again
    assert graph.get_state(config).next == ()                              # finished, no error
    assert agent.streamed == 4 and agent.script == []                      # the closing turn really was streamed, failed, and was asked again
    assert len(academy_world.calendar.events) == 1
    assert [r["to"] for r in read_outbox()] == [PARENT]


def test_without_the_recovery_the_same_run_crashes_with_the_apierror(academy_world, monkeypatch):
    """Guards the test above: with the streamed form no longer recognised, the closing step does crash as reported."""
    monkeypatch.setattr(models, "is_streamed_no_tool_call", lambda exc: False)
    agent = StreamedModel(script=booking_script([streamed_error(), tool_call_message("Done", done=True)]))
    with pytest.raises(groq.APIError):
        run_streamed(agent)
    assert len(academy_world.calendar.events) == 1 and len(read_outbox()) == 1      # what the live run left behind: both written


def test_a_streamed_failure_on_every_try_still_ends_in_the_error_after_the_limit(academy_world):
    agent = StreamedModel(script=booking_script([streamed_error()] * (MAX_NUDGES + 1)))
    with pytest.raises(groq.APIError):
        run_streamed(agent)
    assert len(read_outbox()) == 1                                         # the reply was not sent again by the retries
