"""The end-to-end path with fakes: message -> owner approves -> event in the calendar and reply in the outbox.

Also the helper command `email_assistant.academy.e2e` (polling, results check) against a pretend LangGraph server.
"""

import json

import pytest

from academy_helpers import Run, triage_says
from email_assistant.academy import dates, e2e, settings
from email_assistant.academy.dataset import load_messages
from email_assistant.academy.outbox import read_outbox
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message

SAMPLE = next(m for m in load_messages(e2e.SAMPLES) if m.id == "SAMPLE-04")
PARENT = "parent.a@example.invalid"


def booking_script():
    return ScriptedModel(
        tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-14"),
        tool_call_message("create_demo_event", parent_email=PARENT, child_first_name="Arjun", start="2026-11-04T17:00", end="2026-11-04T17:45"),
        tool_call_message("send_reply", to=PARENT, subject="Your demo class", body="Your demo class is booked for Wednesday 4 Nov at 5 pm."),
        tool_call_message("Done", done=True),
    )


def test_sample_04_is_a_booking_message_that_expects_a_booking():
    assert SAMPLE.expected_label == "book_demo"
    assert SAMPLE.expected_tool_calls == ["find_demo_slots", "create_demo_event", "send_reply"]
    assert SAMPLE.sample and SAMPLE.sender.endswith("@example.invalid>")


def test_a_booking_message_ends_with_an_event_in_the_calendar_and_a_reply_in_the_outbox(academy_world):
    with use_fake_models(triage=triage_says("book_demo"), agent=booking_script()):
        run = Run()
        result = run.start(SAMPLE.graph_input())
        assert run.pending(result)["action_request"]["action"] == "create_demo_event"
        assert academy_world.calendar.events == {} and not academy_world.outbox.exists()  # nothing yet
        result = run.answer(type="accept")
        assert run.pending(result)["action_request"]["action"] == "send_reply"
        assert len(academy_world.calendar.events) == 1 and not academy_world.outbox.exists()  # event yes, reply not yet
        result = run.answer(type="accept")
    assert run.pending(result) is None
    (event,) = academy_world.calendar.events.values()
    assert dates.iso_minutes(event["start"]) == "2026-11-04T17:00"
    assert [r["to"] for r in read_outbox()] == [PARENT]


def test_declining_the_booking_leaves_the_calendar_and_outbox_empty(academy_world):
    agent = ScriptedModel(*booking_script().script[:2], tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        run.start(SAMPLE.graph_input())
        run.answer(type="ignore")
    assert academy_world.calendar.events == {} and not academy_world.outbox.exists()


# ---- the helper command ---------------------------------------------------------------------------

class FakeThreads:
    def __init__(self, statuses, interrupts=None):
        self.statuses, self.interrupts, self.created = list(statuses), interrupts or {}, 0

    def create(self):
        self.created += 1
        return {"thread_id": "t-1"}

    def get(self, thread_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"thread_id": thread_id, "status": status, "interrupts": self.interrupts if status == "interrupted" else {}}


class FakeRuns:
    def __init__(self):
        self.sent = None

    def create(self, thread_id, graph_id, input):
        self.sent = (thread_id, graph_id, input)
        return {"run_id": "r-1"}

    def join(self, thread_id, run_id):
        return {}


class FakeServer:
    def __init__(self, statuses, interrupts=None):
        self.threads, self.runs = FakeThreads(statuses, interrupts), FakeRuns()


INTERRUPT = {"task-1": [{"value": [{"action_request": {"action": "create_demo_event", "args": {"child_first_name": "Arjun"}}}]}]}


def test_pending_actions_gives_names_only():
    thread = {"interrupts": {**INTERRUPT, "task-2": [{"value": [{"action_request": {"action": "send_reply", "args": {"body": "private words"}}}]}]}}
    assert e2e.pending_actions(thread) == ["create_demo_event", "send_reply"]
    assert e2e.pending_actions({"interrupts": {"t": [{"value": "odd"}]}}) == ["a request"] and e2e.pending_actions({}) == []


def test_waiting_announces_each_request_once_and_stops_when_idle():
    server = FakeServer(["interrupted", "interrupted", "busy", "interrupted", "idle"], INTERRUPT)
    said, now = [], [0.0]
    outcome = e2e.wait_for_finish(server, "t-1", timeout=100, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0], say=said.append)
    assert outcome == "idle" and said == ["  Waiting for you in Studio: create_demo_event"]


def test_waiting_reports_errors_and_timeouts():
    now = [0.0]
    sleeper = lambda s: now.__setitem__(0, now[0] + s)  # noqa: E731
    assert e2e.wait_for_finish(FakeServer(["busy", "error"]), "t", timeout=100, sleep=sleeper, clock=lambda: now[0]) == "error"
    now[0] = 0.0
    assert e2e.wait_for_finish(FakeServer(["busy"]), "t", timeout=10, sleep=sleeper, clock=lambda: now[0]) == "timeout"


def test_verify_passes_only_when_both_the_event_and_the_reply_exist(academy_world):
    said = []
    assert e2e.verify(SAMPLE, "Arjun", 0, said.append) is False and sum(line.startswith("FAIL") for line in said) == 2
    academy_world.calendar.create_demo("Demo class - Arjun", dates.parse_local_datetime("2026-11-07T10:00"),
                                       dates.parse_local_datetime("2026-11-07T10:45"), PARENT, "Arjun")
    assert e2e.verify(SAMPLE, "Arjun", 0, said.append) is False  # event but no reply
    academy_world.outbox.write_text(json.dumps({"to": PARENT, "subject": "s", "body": "b"}) + "\n")
    said.clear()
    assert e2e.verify(SAMPLE, "Arjun", 0, said.append) is True and all(line.startswith("PASS") for line in said)
    assert e2e.verify(SAMPLE, "Arjun", 1, said.append) is False  # that reply was already there before the run


def test_replies_that_were_already_in_the_outbox_do_not_count(academy_world):
    academy_world.outbox.write_text(json.dumps({"to": PARENT, "subject": "old", "body": "old"}) + "\n")
    academy_world.calendar.create_demo("Demo class - Arjun", dates.parse_local_datetime("2026-11-07T10:00"),
                                       dates.parse_local_datetime("2026-11-07T10:45"), PARENT, "Arjun")
    assert e2e.verify(SAMPLE, "Arjun", len(read_outbox()), lambda s: None) is False


def ready(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    values = {"TRIAGE_MODEL": "fake:a", "AGENT_MODEL": "fake:b", settings.SUPABASE_URL_VAR: "https://x.supabase.co",
              settings.SUPABASE_ANON_KEY_VAR: "k", settings.CALENDAR_ID_VAR: "cal", settings.TOKEN_FILE_VAR: str(token)}
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def args(**overrides):
    return e2e.argparse.Namespace(**{"sample": "SAMPLE-04", "child": "Arjun", "url": "http://127.0.0.1:2024", "timeout": 5.0, **overrides})


def test_run_sends_the_sample_waits_and_then_checks(academy_world, monkeypatch, tmp_path):
    ready(monkeypatch, tmp_path)
    server = FakeServer(["interrupted", "idle"], INTERRUPT)
    academy_world.calendar.create_demo("Demo class - Arjun", dates.parse_local_datetime("2026-11-07T10:00"),
                                       dates.parse_local_datetime("2026-11-07T10:45"), PARENT, "Arjun")
    academy_world.outbox.write_text("")  # the pretend run "wrote" its reply below
    original_join = server.runs.join

    def join(thread_id, run_id):
        academy_world.outbox.write_text(json.dumps({"to": PARENT, "subject": "s", "body": "b"}) + "\n")
        return original_join(thread_id, run_id)

    server.runs.join = join
    said = []
    monkeypatch.setattr(e2e.time, "sleep", lambda s: None)
    assert e2e.run(args(), client=server, say=said.append) == 0
    thread_id, graph_id, sent = server.runs.sent
    assert (thread_id, graph_id) == ("t-1", "academy_agent") and sent == {"message_input": SAMPLE.graph_input()}
    assert any("accept" in line for line in said) and any(line.startswith("PASS") for line in said)
    assert "Hello, my son" not in "\n".join(said)  # message text is never printed


def test_run_says_what_is_missing_before_doing_anything(academy_world, monkeypatch, tmp_path):
    for name in ("TRIAGE_MODEL", "AGENT_MODEL", settings.SUPABASE_URL_VAR, settings.CALENDAR_ID_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(settings.TOKEN_FILE_VAR, str(tmp_path / "missing.json"))
    said, server = [], FakeServer(["idle"])
    assert e2e.run(args(), client=server, say=said.append) == 1
    text = "\n".join(said)
    assert "TRIAGE_MODEL" in text and settings.SUPABASE_URL_VAR in text and settings.CALENDAR_ID_VAR in text and "google_auth" in text
    assert server.threads.created == 0  # nothing was sent


def test_run_explains_when_the_server_is_not_running(academy_world, monkeypatch, tmp_path):
    ready(monkeypatch, tmp_path)

    class Down:
        class threads:  # noqa: N801
            @staticmethod
            def create():
                raise ConnectionError("refused")

    said = []
    assert e2e.run(args(), client=Down, say=said.append) == 1 and "langgraph dev" in "\n".join(said)


def test_run_refuses_a_sample_that_is_not_a_booking(academy_world, monkeypatch, tmp_path):
    ready(monkeypatch, tmp_path)
    said = []
    assert e2e.run(args(sample="SAMPLE-02"), client=FakeServer(["idle"]), say=said.append) == 1 and "not a book_demo" in said[0]


@pytest.mark.parametrize("sender,address", [("Parent A <parent.a@example.invalid>", PARENT), ("plain@example.invalid", "plain@example.invalid")])
def test_the_parent_address_is_read_from_the_sender(sender, address):
    from dataclasses import replace
    assert e2e.parent_address(replace(SAMPLE, sender=sender)) == address
