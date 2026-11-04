"""Each label goes where the table says. Fake models only."""

import pytest

from academy_helpers import MESSAGE, Run, route, triage_says
from email_assistant.academy.schemas import DRAFTABLE_LABELS, LABELS, RouterSchema
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message


def test_there_are_exactly_the_seven_academy_labels():
    assert LABELS == ("book_demo", "fees_plans", "schedule", "workshop_tournament", "reschedule_cancel", "sensitive", "ignore")


@pytest.mark.parametrize("old_label", ["respond", "notify"])
def test_upstream_labels_are_rejected(old_label):
    with pytest.raises(ValueError):
        RouterSchema(reasoning="x", classification=old_label)


@pytest.mark.parametrize("label", sorted(DRAFTABLE_LABELS))
def test_parent_question_labels_go_to_the_drafter(label):
    agent = ScriptedModel(tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says(label), agent=agent):
        result = Run().start()
    assert result["classification_decision"] == label
    assert len(agent.calls) == 1
    # the drafter was given the parent's message
    assert MESSAGE["text"] in agent.calls[0][1].content
    assert agent.tool_choice == "required"


def test_ignore_ends_without_the_drafter_or_the_owner():
    agent = ScriptedModel()
    with use_fake_models(triage=triage_says("ignore"), agent=agent):
        result = Run().start()
    assert result["classification_decision"] == "ignore"
    assert agent.calls == []
    assert "__interrupt__" not in result


def test_sensitive_goes_to_the_owner_and_ends_there():
    agent = ScriptedModel()
    with use_fake_models(triage=triage_says("sensitive"), agent=agent):
        run = Run()
        result = run.start()
        request = run.pending(result)
        assert request["action_request"]["action"].startswith("Sensitive message")
        assert MESSAGE["text"] in request["description"]
        assert request["config"]["allow_respond"] is False
        run.answer(type="ignore")
    assert run.state.next == ()
    assert agent.calls == []


def test_the_graph_has_no_edge_from_notify_owner_to_the_drafter():
    edges = {(e.source, e.target) for e in Run().graph.get_graph().edges}
    assert ("notify_owner", "response_agent") not in edges
    assert ("notify_owner", "__end__") in edges


def test_a_label_outside_the_table_is_refused():
    bogus = RouterSchema.model_construct(reasoning="x", classification="respond")
    with use_fake_models(triage=ScriptedModel(bogus), agent=ScriptedModel()):
        with pytest.raises(ValueError, match="Invalid classification"):
            Run().start()
