"""Hard rule 5: message text and personal data never reach stdout, stderr or logs."""

import logging
import re
from pathlib import Path

import pytest

from academy_helpers import Run, triage_says
from email_assistant.academy import graph as academy_graph
from email_assistant.models import use_fake_models
from email_assistant.schemas import UserPreferences
from email_assistant.utils import parse_gmail
from fakes import ScriptedModel, tool_call_message

SECRET = "ZX9-PRIVATE-MARKER"
MESSAGE = {"from": f"Parent {SECRET} <{SECRET}@example.invalid>", "text": f"My son {SECRET} wants a demo class."}


def _no_marker(capsys, caplog):
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("label", ["book_demo", "sensitive", "ignore"])
def test_a_run_prints_and_logs_no_message_text(label, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    agent = ScriptedModel(tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says(label), agent=agent):
        run = Run()
        result = run.start(MESSAGE)
        if run.pending(result):
            run.answer(type="ignore")
    _no_marker(capsys, caplog)


def test_an_approved_reply_and_a_memory_update_log_no_message_text(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    send = tool_call_message("send_reply", to=f"{SECRET}@example.invalid", subject="Re", body=f"Hi {SECRET}")
    agent = ScriptedModel(send, UserPreferences(chain_of_thought="c", user_preferences="p"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start(MESSAGE)
        run.answer(type="response", args=f"shorter {SECRET}")
    _no_marker(capsys, caplog)


def test_a_failing_memory_update_logs_only_the_error_type(capsys, caplog):
    send = tool_call_message("send_reply", to="x@example.invalid", subject="Re", body="Hi")
    agent = ScriptedModel(send, RuntimeError(f"boom {SECRET}"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start(MESSAGE)
        run.answer(type="response", args="shorter")
    _no_marker(capsys, caplog)
    assert "RuntimeError" in caplog.text


def test_parse_gmail_prints_nothing(capsys):
    email = {"from": f"a@{SECRET}.invalid", "to": "b", "subject": "s", "body": f"hello {SECRET}", "id": "1"}
    assert parse_gmail(email)[3] == f"hello {SECRET}"
    assert capsys.readouterr().out == ""


def test_the_academy_graph_code_has_no_print_calls():
    folder = Path(academy_graph.__file__).parent
    for name in ("graph.py", "tools.py", "memory.py", "prompts.py", "guards.py", "schemas.py"):
        assert not re.search(r"\bprint\(", (folder / name).read_text(encoding="utf-8")), name
