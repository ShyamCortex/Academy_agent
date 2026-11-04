"""Memory learns reply wording only, and works with the real LangGraph store."""

import pytest
from langgraph.store.memory import InMemoryStore

from academy_helpers import Run, triage_says
from email_assistant.academy.memory import KEY, NAMESPACE, get_preferences, update_preferences
from email_assistant.academy.prompts import default_response_preferences
from email_assistant.models import use_fake_models
from email_assistant.schemas import UserPreferences
from fakes import ScriptedModel, tool_call_message

SEND = dict(to="p@example.invalid", subject="Re", body="Hello")
BOOK = dict(parent_email="p@example.invalid", child_first_name="Arjun", start="2026-11-07T10:00", end="2026-11-07T10:45")


def _learned(text="Sign off with Thanks."):
    return UserPreferences(chain_of_thought="why", user_preferences=text)


def test_the_real_store_rejects_plain_text_which_is_the_upstream_bug():
    with pytest.raises(ValueError):
        InMemoryStore().put(("ns",), "k", "plain text")


def test_first_read_saves_the_default_as_a_dict():
    store = InMemoryStore()
    assert get_preferences(store) == default_response_preferences
    assert store.get(NAMESPACE, KEY).value == {"preferences": default_response_preferences}
    assert get_preferences(store) == default_response_preferences  # second read, same answer


def test_update_rewrites_the_preferences_using_the_agent_model():
    store = InMemoryStore()
    agent = ScriptedModel(_learned("New list"))
    with use_fake_models(agent=agent):
        assert update_preferences(store, "Please sign off with Thanks.") is True
    assert agent.schema is UserPreferences
    assert store.get(NAMESPACE, KEY).value == {"preferences": "New list"}
    assert "wording" in agent.calls[0][0]["content"].lower()  # the instructions say: wording only


def test_a_failing_or_empty_update_keeps_the_old_preferences():
    store = InMemoryStore()
    get_preferences(store)
    with use_fake_models(agent=ScriptedModel(RuntimeError("model is down"))):
        assert update_preferences(store, "feedback") is False
    with use_fake_models(agent=ScriptedModel(_learned("   "))):
        assert update_preferences(store, "feedback") is False
    assert get_preferences(store) == default_response_preferences


def test_editing_a_reply_teaches_wording():
    agent = ScriptedModel(tool_call_message("send_reply", **SEND), _learned("Edited list"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(type="edit", args={"args": {**SEND, "body": "Hello, thanks!"}})
    assert run.store.get(NAMESPACE, KEY).value == {"preferences": "Edited list"}


def test_feedback_on_a_reply_teaches_wording():
    agent = ScriptedModel(tool_call_message("send_reply", **SEND), _learned("Shorter"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(type="response", args="Make it shorter")
    assert run.store.get(NAMESPACE, KEY).value == {"preferences": "Shorter"}


@pytest.mark.parametrize("answer", [
    {"type": "accept"}, {"type": "ignore"},
    {"type": "edit", "args": {"args": BOOK}}, {"type": "response", "args": "Pick another day"},
], ids=lambda a: a["type"])
def test_calendar_decisions_teach_nothing(answer):
    """Calendar edits and "ignore" used to rewrite triage and calendar memory. Not any more."""
    agent = ScriptedModel(tool_call_message("create_demo_event", **BOOK), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        run.start()
        run.answer(**answer)
    assert len(agent.calls) <= 2  # the memory model was never asked
    assert run.store.get(NAMESPACE, KEY).value == {"preferences": default_response_preferences}


def test_ignoring_a_reply_teaches_nothing():
    agent = ScriptedModel(tool_call_message("send_reply", **SEND))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(type="ignore")
    assert len(agent.calls) == 1


def test_the_only_memory_namespace_is_response_preferences():
    agent = ScriptedModel(tool_call_message("send_reply", **SEND), _learned(), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(type="response", args="warmer please")
    assert run.store.list_namespaces() == [NAMESPACE]
