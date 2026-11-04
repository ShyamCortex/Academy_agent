"""Today's date is read on every call, in India time, never once at import."""

import re
from datetime import datetime, timezone

from academy_helpers import Run, triage_says
from email_assistant.academy import prompts
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message


def test_the_prompt_template_holds_no_date():
    assert not re.search(r"\d{4}-\d{2}-\d{2}", prompts.agent_system_prompt)
    assert "{today}" in prompts.agent_system_prompt


def test_today_is_india_time_not_utc():
    late_evening_utc = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)  # 01:30 on the 5th in India
    assert "2026-11-05" in prompts.today_text(late_evening_utc)
    assert prompts.today_text(late_evening_utc).startswith("Thursday")


def test_two_calls_give_two_dates():
    first = prompts.build_agent_prompt("tools", "prefs", datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc))
    second = prompts.build_agent_prompt("tools", "prefs", datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc))
    assert "2026-11-04" in first and "2026-11-20" not in first
    assert "2026-11-20" in second and "2026-11-04" not in second


def test_a_running_graph_sees_the_new_day(monkeypatch):
    """The same compiled graph, a day apart: the drafter is told the new date."""
    class Clock:
        current = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current

    monkeypatch.setattr(prompts, "datetime", Clock)
    seen = []
    for day in (4, 5):
        Clock.current = datetime(2026, 11, day, 6, 0, tzinfo=timezone.utc)
        agent = ScriptedModel(tool_call_message("Done", done=True))
        with use_fake_models(triage=triage_says("schedule"), agent=agent):
            Run().start()
        seen.append(agent.calls[0][0]["content"])
    assert "2026-11-04" in seen[0] and "2026-11-05" in seen[1]
