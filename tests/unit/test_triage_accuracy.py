"""The triage-accuracy script: numbers, results file, rate-limit retry, eval guard."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from academy_helpers import route
from email_assistant.academy import triage_accuracy as ta
from email_assistant.academy.dataset import Message, load_messages
from email_assistant.academy.graph import triage_message
from email_assistant.models import ModelConfigError, use_fake_models
from fakes import ScriptedModel

SECRET = "ZX9-PRIVATE-MARKER"
SAMPLES = Path(__file__).resolve().parents[2] / "data" / "eval" / "sample_messages.jsonl"


def _msg(i, label, text=f"hello {SECRET}"):
    return Message(id=f"m-{i}", split="dev", language="en", text=text, expected_label=label,
                   expected_tool_calls=[], must_include=[], must_not_say=[])


MESSAGES = [_msg(1, "book_demo"), _msg(2, "book_demo"), _msg(3, "fees_plans"), _msg(4, "sensitive")]
PREDICTIONS = {"m-1": "book_demo", "m-2": "fees_plans", "m-3": "fees_plans", "m-4": "sensitive"}


def _run(predictions):
    ids = iter(m.id for m in MESSAGES)
    def classify(_message_input):
        return predictions[next(ids)]
    return ta.run_triage(MESSAGES, classify)


def test_accuracy_overall_and_per_label():
    summary = ta.summarize(_run(PREDICTIONS))
    assert (summary["correct"], summary["total"], summary["accuracy"]) == (3, 4, 0.75)
    assert summary["per_label"]["book_demo"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert summary["per_label"]["fees_plans"]["accuracy"] == 1.0
    assert summary["per_label"]["sensitive"]["accuracy"] == 1.0
    assert "ignore" not in summary["per_label"]  # no ignore messages in this set
    assert summary["confusions"] == [{"expected": "book_demo", "predicted": "fees_plans", "count": 1}]


def test_a_failing_call_counts_as_wrong_and_the_run_goes_on():
    def classify(message_input):
        if message_input["text"].endswith("boom"):
            raise RuntimeError(f"secret detail {SECRET}")
        return "book_demo"
    outcomes = ta.run_triage([_msg(1, "book_demo", "fine"), _msg(2, "book_demo", "boom")], classify)
    summary = ta.summarize(outcomes)
    assert summary["accuracy"] == 0.5 and summary["errors"] == 1
    assert outcomes[1]["error"] == "RuntimeError"  # the type only
    assert SECRET not in json.dumps(outcomes)


def test_the_report_shows_overall_and_each_label():
    report = ta.format_report(ta.summarize(_run(PREDICTIONS)))
    assert "3/4 = 75%" in report
    for label in ("book_demo", "fees_plans", "sensitive"):
        assert label in report
    assert "book_demo -> fees_plans: 1" in report


def test_the_results_file_is_dated_and_holds_no_message_text(tmp_path):
    outcomes = _run(PREDICTIONS)
    when = datetime(2026, 11, 8, 9, 30, 15)
    path = ta.save_results(ta.summarize(outcomes), outcomes, "data/eval/x.jsonl", tmp_path / "results", now=when)
    assert re.fullmatch(r"triage_accuracy_20261108_093015\.json", path.name)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["date"].startswith("2026-11-08") and saved["summary"]["total"] == 4
    assert SECRET not in path.read_text(encoding="utf-8")


def test_rate_limits_are_retried(monkeypatch):
    class RateLimited(Exception):
        status_code = 429

    sleeps = []
    monkeypatch.setattr("email_assistant.models._sleep", sleeps.append)
    triage = ScriptedModel(RateLimited("429 Too Many Requests"), RateLimited("429 Too Many Requests"), route("book_demo"))
    with use_fake_models(triage=triage):
        assert triage_message({"from": "p", "text": "demo please"}) == "book_demo"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # waited twice, longer the second time


def test_main_end_to_end_on_the_samples(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    sample = tmp_path / "s.jsonl"
    sample.write_text(SAMPLES.read_text(encoding="utf-8"), encoding="utf-8")
    labels = {m.id: m.expected_label for m in load_messages(sample)}
    ids = iter(labels)
    assert ta.main([str(sample), "--out-dir", str(tmp_path / "out")], classify=lambda _m: labels[next(ids)]) == 0
    out = capsys.readouterr().out
    assert "5/5 = 100%" in out and "Saved:" in out
    assert len(list((tmp_path / "out").glob("triage_accuracy_*.json"))) == 1


def test_main_refuses_when_judge_equals_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_MODEL", "fake:same-model")
    monkeypatch.setenv("JUDGE_MODEL", "fake:same-model")
    with pytest.raises(ModelConfigError, match="JUDGE_MODEL"):
        ta.main(["data/eval/sample_messages.jsonl"], classify=lambda _m: "ignore")


def test_main_stops_with_a_clear_error_when_triage_model_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("TRIAGE_MODEL", "AGENT_MODEL", "JUDGE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ModelConfigError, match="TRIAGE_MODEL is not set"):
        ta.main([str(SAMPLES), "--out-dir", str(tmp_path)])


def test_main_exits_with_an_error_when_every_call_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)

    def always_fails(_message_input):
        raise RuntimeError("down")

    assert ta.main([str(SAMPLES), "--out-dir", str(tmp_path / "out")], classify=always_fails) == 1
    assert "not a real accuracy" in capsys.readouterr().err
