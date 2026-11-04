"""Structured output on Groq: a 400 `tool_use_failed`, no answer, or a bad value is re-asked (at most twice);
errors are described by type and provider code only; the judge's quality is a plain int checked in code."""

import groq
import httpx
import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from email_assistant import models
from email_assistant.academy import prompts
from email_assistant.academy.dataset import Message
from email_assistant.academy.judge import JudgeGrade, judge_reply
from email_assistant.models import (
    MAX_NUDGES, NoStructuredAnswerError, describe_error, error_code, structured_answer_nudge, structured_llm, use_fake_models,
)
from fakes import ScriptedModel

PRIVATE = "ZQX-PRIVATE-TEXT"


def groq_error(status=400, code="tool_use_failed", message="tool call validation failed: parameters for tool JudgeGrade did not match schema"):
    """A real Groq SDK error, built the way the SDK builds it from the HTTP answer."""
    request = httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions")
    body = {"error": {"message": f"{message} {PRIVATE}", "type": "invalid_request_error", "code": code,
                      "failed_generation": f"{PRIVATE} quality five"}}
    cls = {400: groq.BadRequestError, 429: groq.RateLimitError}[status]
    return cls(f"Error code: {status} - {body}", response=httpx.Response(status, request=request), body=body)


def grade(quality=4):
    return JudgeGrade(reasoning="ok", quality=quality, must_include_met=True, must_not_say_violated=False, gives_chess_advice=False)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(models, "_sleep", sleeps.append)
    monkeypatch.setenv("MODEL_RETRY_INITIAL_SECONDS", "0")
    return sleeps


def ask_judge(*script):
    fake = ScriptedModel(*script)
    with use_fake_models(judge=fake):
        answer = structured_llm("judge", JudgeGrade).invoke([{"role": "user", "content": "grade it"}])
    return fake, answer


# ---- recovery, like tool_llm ----------------------------------------------------------------------

def test_a_groq_tool_use_failed_400_is_re_asked_with_a_reminder(no_sleep):
    fake, answer = ask_judge(groq_error(), grade(5))
    assert answer.quality == 5 and len(fake.calls) == 2
    reminder = fake.calls[1][-1]
    assert reminder["role"] == "user" and "JudgeGrade" in reminder["content"]
    assert PRIVATE not in reminder["content"]          # the reminder never repeats the error text or the model's output
    assert no_sleep == []                              # a model mistake is not waited on


def test_no_answer_at_all_is_re_asked():
    fake, answer = ask_judge(None, grade(3))
    assert answer.quality == 3 and len(fake.calls) == 2


def test_a_bad_value_caught_by_our_own_check_is_re_asked():
    def out_of_range():
        try:
            JudgeGrade(reasoning="x", quality=7, must_include_met=True, must_not_say_violated=False, gives_chess_advice=False)
        except ValidationError as exc:
            return exc
    fake, answer = ask_judge(out_of_range(), grade(2))
    assert answer.quality == 2 and len(fake.calls) == 2


def test_it_gives_up_after_two_reminders_and_raises_the_groq_error():
    fake = ScriptedModel(*[groq_error()] * (MAX_NUDGES + 2))
    with use_fake_models(judge=fake), pytest.raises(groq.BadRequestError):
        structured_llm("judge", JudgeGrade).invoke([{"role": "user", "content": "grade it"}])
    assert len(fake.calls) == MAX_NUDGES + 1


def test_other_400s_are_not_re_asked():
    fake = ScriptedModel(groq_error(code="model_not_found", message="no such model"), grade())
    with use_fake_models(judge=fake), pytest.raises(groq.BadRequestError):
        structured_llm("judge", JudgeGrade).invoke([{"role": "user", "content": "grade it"}])
    assert len(fake.calls) == 1


def test_a_rate_limit_is_still_waited_on_not_nudged(no_sleep):
    fake, answer = ask_judge(groq_error(status=429, code="rate_limit_exceeded", message="slow down"), grade())
    assert answer.quality == 4 and len(no_sleep) == 1 and fake.calls[1] == fake.calls[0]


def test_triage_and_memory_get_the_same_recovery():
    from email_assistant.schemas import RouterSchema

    fake = ScriptedModel(groq_error(), RouterSchema(reasoning="r", classification="respond"))
    with use_fake_models(triage=fake):
        assert structured_llm("triage", RouterSchema).invoke([{"role": "user", "content": "x"}]).classification == "respond"
    assert "RouterSchema" in fake.calls[1][-1]["content"]


def test_the_nudge_only_answers_known_mistakes():
    nudge = structured_answer_nudge("JudgeGrade")
    assert nudge(NoStructuredAnswerError("x")) and nudge(groq_error())
    assert nudge(groq_error(code="invalid_api_key", message="bad key")) is None
    assert nudge(RuntimeError("boom")) is None


# ---- describing an error without its text -----------------------------------------------------------

def test_the_groq_code_is_read_from_the_error_body():
    assert error_code(groq_error()) == "tool_use_failed"
    assert describe_error(groq_error()) == "BadRequestError, code tool_use_failed"


def test_the_code_is_found_on_a_cause_and_in_text_only_sdks():
    try:
        try:
            raise groq_error(code="json_validate_failed")
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert error_code(outer) == "json_validate_failed"
    text_only = Exception("Error code: 400 - {'error': {'message': 'x', 'type': 'invalid_request_error', 'code': 'tool_use_failed'}}")
    assert error_code(text_only) == "tool_use_failed"


def test_no_code_means_just_the_type_and_message_text_never_appears():
    assert describe_error(ValueError(f"{PRIVATE} said hello")) == "ValueError"
    assert PRIVATE not in describe_error(groq_error())


def test_a_code_that_is_not_a_short_identifier_is_ignored():
    class Odd(Exception):
        code = f"{PRIVATE} has spaces and is long"
    assert error_code(Odd("x")) is None


# ---- the judge's schema ------------------------------------------------------------------------------

def test_quality_is_a_plain_integer_in_the_schema_with_no_enum_or_range():
    quality = convert_to_openai_tool(JudgeGrade)["function"]["parameters"]["properties"]["quality"]
    assert quality["type"] == "integer"
    assert not {"enum", "minimum", "maximum", "const", "anyOf"} & set(quality)


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_quality_outside_one_to_five_is_refused_in_code(bad):
    with pytest.raises(ValidationError):
        grade(bad)


@pytest.mark.parametrize("good", [1, 2, 3, 4, 5])
def test_quality_one_to_five_is_accepted(good):
    assert grade(good).quality == good


def test_judge_reply_recovers_from_a_groq_400():
    message = Message(id="T-1", split="dev", language="en", text="How many classes?", expected_label="fees_plans",
                      expected_tool_calls=["get_plans"], must_include=[], must_not_say=[])
    fake = ScriptedModel(groq_error(), grade(4))
    with use_fake_models(judge=fake):
        assert judge_reply(message, [{"subject": "Re", "body": "4 classes a month."}], [("get_plans", "{}")]).quality == 4


# ---- the response prompt: numbers exactly as the tool gives them ---------------------------------------

def test_the_agent_prompt_says_to_quote_plan_numbers_per_month_and_never_convert():
    text = prompts.agent_system_prompt
    assert "classes_per_month" in text and "never a number of\n   classes a week" in text
    assert "Never convert" in text and "never compute a new number" in text


def test_the_get_plans_tool_says_classes_are_per_month():
    from email_assistant.academy.data_tools import get_plans

    assert "per month, not per week" in get_plans.description
