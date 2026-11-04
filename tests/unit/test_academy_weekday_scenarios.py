"""The failures of the v1 dev eval, as general scenarios on the real graph, the real tools and the real scorers.

The v1 replies wrote weekdays and dates the model worked out itself ("Tuesday, 7 Nov" when 7 Nov is a Saturday, a fourth
Saturday that no tool returned) and said a free slot was not available. Here a scripted agent writes such replies and the
scorers must catch them, and a reply that copies the tool's labels must pass. The clock is Wednesday 2026-11-04.
"""

import json

from academy_helpers import triage_says
from email_assistant.academy import prompts, tools as academy_tools
from email_assistant.academy.dataset import Message
from email_assistant.academy.eval_harness import run_message
from email_assistant.academy.eval_scorers import check_numbers, check_tools, score_trace
from email_assistant.academy.graph import build_graph
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message

SENDER = "Lakshmi <lakshmi@example.invalid>"
EMAIL = "lakshmi@example.invalid"


def message(label="book_demo", tools=("find_demo_slots", "create_demo_event", "send_reply"), **kw) -> Message:
    base = dict(id="T-1", split="dev", language="en", text="Please book a demo for Karthik on Monday at 6 pm.", expected_label=label,
                expected_tool_calls=list(tools), must_include=[], must_not_say=[], sender=SENDER)
    return Message(**{**base, **kw})


def run(*calls, row=None):
    agent = ScriptedModel(*calls, tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        return run_message(build_graph(), row or message())


SEARCH = tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-04")


def book_monday_6pm():
    return tool_call_message("create_demo_event", parent_email=EMAIL, child_first_name="Karthik", start="2026-11-09T18:00", end="2026-11-09T18:45")


def reply(body: str):
    return tool_call_message("send_reply", to=EMAIL, subject="Your demo class", body=body)


def test_a_reply_that_copies_the_tools_labels_passes_every_check(academy_world):
    trace = run(SEARCH, book_monday_6pm(), reply("Karthik's demo (free, 45 minutes) is booked for Monday 2026-11-09 18:00 IST."))
    assert check_numbers(trace) == {"checked": True, "ungrounded": [], "wrong_weekday": []}
    assert check_tools(message(), trace)["pass"] is True
    assert [json.loads(text)["booked"] for name, text in trace.outputs if name == "create_demo_event"] == [True]


def test_a_requested_weekday_and_time_is_in_the_first_search_without_the_model_counting_dates(academy_world):
    """v1: the model asked for 4-10 Nov, did not see Monday 18:00 and said it was not available. It is in the answer now."""
    trace = run(SEARCH, reply("x"))
    labels = [s["label"] for s in json.loads(trace.outputs[0][1])["free_slots"]]
    assert "Monday 2026-11-09 18:00 IST" in labels and "Wednesday 2026-11-11 17:00 IST" in labels


def test_a_weekday_the_model_worked_out_itself_is_flagged(academy_world):
    trace = run(SEARCH, reply("We have Tuesday, 7 Nov at 10:00 and Wednesday, 9 Nov at 5 pm."))
    result = check_numbers(trace)
    assert result["wrong_weekday"] == ["Tuesday, 7 Nov", "Wednesday, 9 Nov"]
    assert result["ungrounded"] == []                              # 7 Nov 10:00 is a real slot, 9 Nov 17:00 is a real slot: only the weekdays are wrong
    row = score_trace(message(tools=("find_demo_slots", "send_reply")), trace)
    assert row["numbers"]["wrong_weekday"] and row["numbers"]["checked"]


def test_a_date_beyond_what_the_tool_returned_is_flagged(academy_world):
    """v1: 'Saturdays 7, 14, 21 and 28 November' when the tool had listed only some of them."""
    trace = run(tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-04", weekday="Saturday"),
                reply("Saturday demo slots: 7, 14, 21 and 28 November at 10:00 and 11:30."))
    assert check_numbers(trace)["ungrounded"] == ["28 November"]    # 7, 14, 21 Nov are the three Saturdays the tool returned


def test_a_time_written_as_pm_matches_the_24_hour_slot(academy_world):
    """v1: '5 pm' was reported as not in the tool output although the tool said 17:00."""
    trace = run(SEARCH, reply("Wednesday 11 Nov at 5 pm works, and Monday 9 Nov at 6 pm."))
    assert check_numbers(trace) == {"checked": True, "ungrounded": [], "wrong_weekday": []}


def test_a_time_no_slot_has_is_flagged(academy_world):
    trace = run(SEARCH, reply("Monday 2026-11-09 at 7 pm."))
    assert check_numbers(trace)["ungrounded"] == ["7 pm"]


# ---- the prompts ------------------------------------------------------------------------------------------------

def squashed(text: str) -> str:
    return " ".join(text.split())


def test_the_agent_prompt_forbids_computing_weekdays_and_dates_and_says_to_book_a_slot_that_is_listed():
    text = squashed(prompts.agent_system_prompt)
    for part in (
        "Never work out a weekday or a date yourself", "copy them from tool results",
        "Offer, confirm, book and move only demo slots that find_demo_slots returned",
        "it IS free: book it (create_demo_event) or move to it (reschedule_demo), do not say it is unavailable",
        "only when no slot with that weekday and time is in the list",
        "demo_terms", "Use plan, batch, workshop and tournament names exactly as the tool returned them",
        "name the workshop or tournament in the reply",
        "reschedule_demo with that slot's start as new_start and its end as new_end",
    ):
        assert part in text, part


def test_the_prompts_hold_no_academy_facts():
    for text in (prompts.agent_system_prompt, academy_tools.TOOLS_PROMPT.replace("2026-11-07T10:00", "")):
        assert "free, 45" not in text and "45 minutes" not in text
        assert not __import__("re").search(r"\d{4}-\d{2}-\d{2}", text)


def test_the_tool_list_tells_the_model_what_find_demo_slots_gives_and_takes():
    text = squashed(academy_tools.TOOLS_PROMPT)
    for part in ("find_demo_slots(date_from, date_to, weekday)", "weekday, date and 24 h time", "next 3 occurrences of every demo day",
                 "demo_terms", "weekday is optional"):
        assert part in text, part
