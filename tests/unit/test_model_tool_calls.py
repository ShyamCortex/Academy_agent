"""Tool-calling mistakes seen with gpt-oss on Groq, and the eval guard for the judge model."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from email_assistant import models
from email_assistant.academy.tools import ACADEMY_TOOLS
from email_assistant.models import ModelConfigError, is_retryable, require_distinct_judge, tool_call_nudge, tool_llm, use_fake_models
from email_assistant.tools.default.calendar_tools import schedule_meeting
from fakes import ScriptedModel, tool_call_message

ROOT = Path(__file__).resolve().parents[2]

# The three kinds of error the laptop run produced (message text copied from the run).
NO_TOOL_ERROR = ("Error code: 400 - {'error': {'message': 'Tool choice is required, but model did not call a tool', "
                 "'type': 'invalid_request_error', 'code': 'tool_use_failed', "
                 "'failed_generation': 'The email has been sent and the task is complete.'}}")
BAD_ARGS_ERROR = ("Error code: 400 - {'error': {'message': \"Tool call validation failed: tool call validation failed: "
                  "parameters for tool schedule_meeting did not match schema: errors: [`/preferred_day`: "
                  "'2026-11-07T14:00' is not valid date-time: invalid date-time]\", "
                  "'type': 'invalid_request_error', 'code': 'tool_use_failed'}}")


class BadRequest(Exception):
    status_code = 400


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(models, "_sleep", sleeps.append)
    return sleeps


def _ask(*script):
    fake = ScriptedModel(*script)
    with use_fake_models(agent=fake):
        reply = tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    return fake, reply


def test_known_errors_get_a_reminder_and_other_errors_do_not():
    assert tool_call_nudge(BadRequest(NO_TOOL_ERROR)) == models.NUDGE_NO_TOOL
    assert tool_call_nudge(BadRequest(BAD_ARGS_ERROR)) == models.NUDGE_BAD_ARGS
    assert tool_call_nudge(models.NoToolCallError("x")) == models.NUDGE_NO_TOOL
    assert tool_call_nudge(BadRequest("Error code: 400 - invalid api key")) is None
    assert tool_call_nudge(BadRequest("Error code: 429 - rate limit")) is None


def test_a_forced_call_answered_with_a_400_is_repeated_with_a_reminder(no_sleep):
    fake, reply = _ask(BadRequest(NO_TOOL_ERROR), tool_call_message("Done", done=True))
    assert reply.tool_calls[0]["name"] == "Done"
    assert len(fake.calls) == 2
    assert fake.calls[1][-1] == {"role": "user", "content": models.NUDGE_NO_TOOL}
    assert fake.calls[0] == [{"role": "user", "content": "go"}]  # the first try was not changed
    assert no_sleep == []  # waiting does not help, so we did not wait


def test_a_forced_call_answered_with_plain_text_is_repeated_with_a_reminder(no_sleep):
    fake, reply = _ask(AIMessage(content="The email has been sent."), tool_call_message("Done", done=True))
    assert reply.tool_calls and fake.calls[1][-1]["content"] == models.NUDGE_NO_TOOL


def test_bad_arguments_get_the_schema_reminder(no_sleep):
    fake, reply = _ask(BadRequest(BAD_ARGS_ERROR), tool_call_message("Done", done=True))
    assert fake.calls[1][-1]["content"] == models.NUDGE_BAD_ARGS


def test_the_reminder_never_repeats_the_models_output_or_the_arguments(no_sleep):
    fake, _ = _ask(BadRequest(NO_TOOL_ERROR), BadRequest(BAD_ARGS_ERROR), tool_call_message("Done", done=True))
    reminders = " ".join(call[-1]["content"] for call in fake.calls[1:])
    assert "email has been sent" not in reminders and "2026-11-07" not in reminders


def test_it_gives_up_after_the_limit(no_sleep):
    errors = [BadRequest(NO_TOOL_ERROR)] * (models.MAX_NUDGES + 1)
    fake = ScriptedModel(*errors)
    with use_fake_models(agent=fake), pytest.raises(BadRequest):
        tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert len(fake.calls) == models.MAX_NUDGES + 1


def test_an_unrelated_400_is_raised_at_once(no_sleep):
    fake = ScriptedModel(BadRequest("Error code: 400 - invalid api key"))
    with use_fake_models(agent=fake), pytest.raises(BadRequest):
        tool_llm("agent", [], tool_choice="required").invoke([{"role": "user", "content": "go"}])
    assert len(fake.calls) == 1


def test_plain_text_is_fine_when_no_tool_is_forced():
    fake = ScriptedModel(AIMessage(content="hello"))
    with use_fake_models(agent=fake):
        assert tool_llm("agent", [], tool_choice=None).invoke([{"role": "user", "content": "go"}]).content == "hello"


def test_a_tool_use_failed_error_is_not_retried_by_waiting():
    # the words "unavailable" and "rate limit" inside the model's own output must not trigger a wait
    assert not is_retryable(BadRequest(NO_TOOL_ERROR.replace("task is complete", "calendar unavailable, rate limit")))
    assert is_retryable(BadRequest("Error code: 429 - rate limit reached"))


def _schema_text(tool) -> str:
    return json.dumps(convert_to_openai_tool(tool))


def test_no_academy_tool_takes_a_datetime_argument():
    """Groq checks "date-time" strictly and rejects the model's "2026-11-07T14:00"."""
    assert '"date-time"' in _schema_text(schedule_meeting)  # the upstream tool has the problem (the check can see it)
    for t in ACADEMY_TOOLS:
        assert '"date-time"' not in _schema_text(t), t.name


# ---- eval guard: the judge must not be the drafter -----------------------------------------

def test_the_same_model_for_judge_and_agent_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "fake:big-model-120b")
    monkeypatch.setenv("JUDGE_MODEL", "fake:big-model-120b")
    with pytest.raises(ModelConfigError) as error:
        require_distinct_judge()
    assert "JUDGE_MODEL" in str(error.value) and "AGENT_MODEL" in str(error.value)
    assert "fake:big-model-120b" in str(error.value)


def test_capitals_and_spaces_do_not_hide_the_same_model(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "fake:big-model-120b")
    monkeypatch.setenv("JUDGE_MODEL", "  Fake:Big-Model-120B ")
    with pytest.raises(ModelConfigError):
        require_distinct_judge()


@pytest.mark.parametrize("agent,judge", [
    ("fake:big-model-120b", "fake:other-model-27b"),  # different models: fine
    ("fake:big-model-120b", ""),                        # judge not set: the missing-variable error comes later
    ("", "fake:other-model-27b"),
    ("", ""),
])
def test_different_or_unset_models_pass(monkeypatch, agent, judge):
    monkeypatch.setenv("AGENT_MODEL", agent)
    monkeypatch.setenv("JUDGE_MODEL", judge)
    require_distinct_judge()


def test_the_existing_eval_entry_points_call_the_guard():
    for path in ("tests/test_response.py", "src/email_assistant/eval/evaluate_triage.py", "src/email_assistant/academy/triage_accuracy.py"):
        assert "require_distinct_judge()" in (ROOT / path).read_text(encoding="utf-8"), path


def test_the_real_response_tests_refuse_to_start(tmp_path):
    env = {**os.environ, "AGENT_MODEL": "fake:same", "JUDGE_MODEL": "fake:same", "LANGSMITH_TRACING": "false"}
    done = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_response.py"), "--collect-only", "-q", "-p", "no:cacheprovider"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode != 0
    assert "JUDGE_MODEL and AGENT_MODEL are both" in done.stdout + done.stderr
