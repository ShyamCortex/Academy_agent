"""Approved replies go to a local outbox file, and the files that hold secrets or messages are git-ignored."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from academy_helpers import Run, triage_says
from email_assistant.academy import settings
from email_assistant.academy.outbox import outbox_path, read_outbox, send_reply
from email_assistant.models import use_fake_models
from email_assistant.schemas import UserPreferences
from fakes import ScriptedModel, tool_call_message

ROOT = Path(__file__).resolve().parents[2]
REPLY = {"to": "parent.a@example.invalid", "subject": "Your demo class", "body": "Hello! Your demo is booked."}


def test_a_reply_is_appended_as_one_json_line(academy_world):
    first = json.loads(send_reply.invoke(REPLY))
    send_reply.invoke({**REPLY, "subject": "Second"})
    assert first["queued"] is True and first["source"].startswith("local outbox")
    lines = academy_world.outbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert (record["to"], record["subject"], record["body"]) == (REPLY["to"], REPLY["subject"], REPLY["body"])
    assert record["id"] == first["outbox_id"] and record["at"].endswith("+05:30")
    assert [r["subject"] for r in read_outbox()] == ["Your demo class", "Second"]


def test_the_outbox_folder_is_created_when_missing(academy_world, monkeypatch, tmp_path):
    monkeypatch.setenv(settings.OUTBOX_FILE_VAR, str(tmp_path / "new" / "folder" / "out.jsonl"))
    send_reply.invoke(REPLY)
    assert (tmp_path / "new" / "folder" / "out.jsonl").exists()


@pytest.mark.parametrize("bad", [{**REPLY, "to": "not-an-address"}, {**REPLY, "body": "   "}])
def test_a_reply_with_no_address_or_no_text_is_not_queued(academy_world, bad):
    assert json.loads(send_reply.invoke(bad))["queued"] is False
    assert not academy_world.outbox.exists()


def test_the_default_outbox_path_is_the_git_ignored_one(monkeypatch):
    monkeypatch.delenv(settings.OUTBOX_FILE_VAR)
    assert outbox_path() == Path("outbox/academy_outbox.jsonl")


def test_nothing_reaches_the_outbox_until_the_owner_accepts(academy_world):
    agent = ScriptedModel(tool_call_message("send_reply", **REPLY), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        assert run.pending(run.start())["action_request"]["action"] == "send_reply"
        assert not academy_world.outbox.exists()  # waiting: nothing written
        run.answer(type="accept")
    assert [r["to"] for r in read_outbox()] == [REPLY["to"]]


def _learned(text="New list"):
    return UserPreferences(chain_of_thought="why", user_preferences=text)


@pytest.mark.parametrize("answer", [{"type": "ignore"}, {"type": "response", "args": "not like this"}])
def test_a_declined_reply_never_reaches_the_outbox(academy_world, answer):
    # after "response" the memory update uses the agent model once (the second scripted answer), then the draft continues
    agent = ScriptedModel(tool_call_message("send_reply", **REPLY), _learned(), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(**answer)
    assert not academy_world.outbox.exists()


def test_an_edited_reply_is_queued_as_edited(academy_world):
    agent = ScriptedModel(tool_call_message("send_reply", **REPLY), _learned(), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        run = Run()
        run.start()
        run.answer(type="edit", args={"args": {**REPLY, "body": "Edited by the owner"}})
    assert [r["body"] for r in read_outbox()] == ["Edited by the owner"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("path", [
    ".env", ".secrets/academy_google_credentials.json", ".secrets/academy_google_token.json",
    "client_secret_123.apps.googleusercontent.com.json", "outbox/academy_outbox.jsonl", "token.json",
    "somewhere/else/academy_google_token.json",
])
def test_secret_and_message_files_are_git_ignored(path):
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert result.returncode == 0, f"{path} is not git-ignored"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_no_secret_or_outbox_file_is_tracked():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    bad = [f for f in tracked if f.startswith(("outbox/", ".secrets/")) or f == ".env" or "google_token" in f or f.startswith("client_secret")]
    assert bad == []
