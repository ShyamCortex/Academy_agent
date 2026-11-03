"""Run the basic email assistant end to end on fake models (no API keys, no network)."""

import pytest

from email_assistant.eval.email_dataset import email_input_1
from email_assistant.models import use_fake_models
from email_assistant.schemas import RouterSchema, UserPreferences
from email_assistant.utils import extract_tool_calls
from fakes import ScriptedModel, tool_call_message


def _route(classification):
    return RouterSchema(reasoning="test", classification=classification)


def test_respond_email_gets_a_draft_then_done():
    from email_assistant.email_assistant import email_assistant

    triage = ScriptedModel(_route("respond"))
    agent = ScriptedModel(
        tool_call_message("write_email", to="alice.smith@company.com", subject="Re: docs", content="I will look into it."),
        tool_call_message("Done", done=True),
    )
    with use_fake_models(triage=triage, agent=agent):
        result = email_assistant.invoke({"email_input": email_input_1})

    assert result["classification_decision"] == "respond"
    assert extract_tool_calls(result["messages"]) == ["write_email", "done"]
    assert "Email sent to alice.smith@company.com" in result["messages"][2].content  # the tool really ran
    assert agent.tool_choice == "any"  # this graph asks the model to always use a tool


@pytest.mark.parametrize("classification", ["ignore", "notify"])
def test_ignore_and_notify_never_reach_the_agent(classification):
    from email_assistant.email_assistant import email_assistant

    agent = ScriptedModel(tool_call_message("write_email", to="x", subject="x", content="x"))
    with use_fake_models(triage=ScriptedModel(_route(classification)), agent=agent):
        result = email_assistant.invoke({"email_input": email_input_1})

    assert result["classification_decision"] == classification
    assert agent.calls == []  # the drafting model was never called


class _TextStore:
    """Tiny stand-in for a LangGraph store that keeps plain-text values, the way the upstream
    memory code writes them. (The real InMemoryStore in langgraph 1.x rejects text values:
    a known upstream problem, fixed when the academy graph rewrites memory in Phase 1.)"""

    class _Item:
        def __init__(self, value):
            self.value = value

    def __init__(self):
        self.data = {}

    def get(self, namespace, key):
        return self._Item(self.data[(namespace, key)]) if (namespace, key) in self.data else None

    def put(self, namespace, key, value):
        self.data[(namespace, key)] = value


def test_memory_update_uses_the_agent_model():
    """update_memory used to build its own OpenAI model; now it goes through AGENT_MODEL."""
    from email_assistant.email_assistant_hitl_memory import update_memory

    namespace = ("email_assistant", "response_preferences")
    store = _TextStore()
    store.put(namespace, "user_preferences", "old profile")
    agent = ScriptedModel(UserPreferences(chain_of_thought="why", user_preferences="new profile"))

    with use_fake_models(agent=agent):
        update_memory(store, namespace, [{"role": "user", "content": "Please sign off with Thanks."}])

    assert agent.schema is UserPreferences
    assert store.get(namespace, "user_preferences").value == "new profile"
