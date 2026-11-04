"""The eval runner end to end, with fake models: batches, resume, the judge, the held-out rules and the saved files."""

import json
from datetime import datetime

import pytest

from academy_helpers import route
from email_assistant.academy import config, safety_eval
from email_assistant.academy.graph import build_graph
from email_assistant.academy.judge import JudgeGrade
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message

NOW = datetime(2026, 11, 4, 15, 30, tzinfo=config.TIMEZONE)
SECRET_TEXT = "ZQX-MESSAGE-TEXT"
REPLY_BODY = "Hello. The demo class is free and lasts 45 minutes."


def row_for(id, label, tools, split="dev", language="en", sample=False):
    row = {"id": id, "split": split, "language": language, "text": f"{SECRET_TEXT} {id}", "expected_label": label,
           "expected_tool_calls": tools, "must_include": ["It is free"], "must_not_say": ["Any chess advice"]}
    return {**row, "sample": True} if sample else row


BOOK = ["find_demo_slots", "send_reply"]


def write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def good_grade(quality=5, chess=False):
    return JudgeGrade(reasoning="fine", quality=quality, must_include_met=True, must_not_say_violated=False, gives_chess_advice=chess)


def drafting(n=1):
    """The agent's script for n messages that are each looked up, answered and finished."""
    script = []
    for _ in range(n):
        script += [tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-10"),
                   tool_call_message("send_reply", to="parent@example.invalid", subject="Demo", body=REPLY_BODY),
                   tool_call_message("Done", done=True)]
    return script


@pytest.fixture
def env(monkeypatch, tmp_path):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)   # a laptop .env must not leak into tests
    monkeypatch.setenv("TRIAGE_MODEL", "fake:triage-small")
    monkeypatch.setenv("AGENT_MODEL", "fake:agent-big")
    monkeypatch.setenv("JUDGE_MODEL", "fake:judge-other")
    monkeypatch.setenv("MODEL_RETRY_INITIAL_SECONDS", "0")
    monkeypatch.setattr("email_assistant.models._sleep", lambda s: None)
    return tmp_path


def cli(env, dataset, *extra, judge_script=(), triage_script=(), agent_script=(), sleeps=None):
    said = []
    out = env / "results"
    args = ["--dataset", str(dataset), "--out-dir", str(out), "--private-dir", str(env / "private"), "--pause", "7",
            "--my-scores", str(env / "my_scores.jsonl"), *extra]
    with use_fake_models(triage=ScriptedModel(*[route(label) for label in triage_script]), agent=ScriptedModel(*agent_script),
                         judge=ScriptedModel(*judge_script)):
        code = safety_eval.main(args, workflow=build_graph(), sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                                now=lambda: NOW, say=said.append)
    return code, "\n".join(said), out


def saved_rows(out, run_id="dev-20261104"):
    """Every line of the resume file. A message appears twice when it was judged in a second step (latest line wins)."""
    return [json.loads(line) for line in (out / f"eval_{run_id}.jsonl").read_text(encoding="utf-8").splitlines()]


def latest(out, run_id="dev-20261104"):
    return {r["id"]: r for r in saved_rows(out, run_id)}


DEV = lambda env: write(env / "dev.jsonl", [row_for("d1", "book_demo", BOOK), row_for("d2", "sensitive", []), row_for("d3", "ignore", [])])  # noqa: E731


# ---- a full run ------------------------------------------------------------------------------------

def test_a_dev_run_scores_every_message_and_writes_the_three_result_files(env):
    code, output, out = cli(env, DEV(env), triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade()])
    assert code == 0, output
    assert sorted(p.name for p in out.iterdir()) == ["eval_dev-20261104.json", "eval_dev-20261104.jsonl", "eval_dev-20261104.md"]
    summary = json.loads((out / "eval_dev-20261104.json").read_text(encoding="utf-8"))["summary"]
    assert summary["triage"]["accuracy"] == 1.0 and summary["sensitive"]["recall"] == 1.0 and summary["sensitive"]["never_drafted"] == 1.0
    assert summary["numbers"]["rate"] == 1.0 and summary["tools"]["rate"] == 1.0 and summary["quality"]["mean"] == 5.0
    assert all(t["met"] for t in summary["targets"])
    table = (out / "eval_dev-20261104.md").read_text(encoding="utf-8")
    for heading in ("Pass bars", "Triage accuracy", "Sensitive messages", "Invented numbers", "Tool calls", "Reply quality", "Judge vs your scores", "Cost and latency"):
        assert heading in table
    assert "PASS" in table and "FAIL" not in table and "fake:agent-big" in table


def test_no_message_text_or_reply_text_is_saved_in_the_results(env):
    _, _, out = cli(env, DEV(env), triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade()])
    for path in out.iterdir():
        text = path.read_text(encoding="utf-8")
        assert SECRET_TEXT not in text and REPLY_BODY not in text and "parent@example.invalid" not in text, path.name


def test_replies_stay_in_the_local_private_files_for_you_to_read(env):
    cli(env, DEV(env), triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade(4)])
    private = json.loads((env / "private" / "eval_private_dev-20261104.json").read_text(encoding="utf-8"))
    assert private["d1"]["drafts"][0]["body"] == REPLY_BODY and private["d1"]["judge_reasoning"] == "fine"
    review = (env / "private" / "eval_review_dev-20261104.md").read_text(encoding="utf-8")
    assert "d1" in review and REPLY_BODY in review and "Judge: 4/5" in review and "d2" not in review   # only drafted replies


def test_approved_replies_go_to_an_eval_only_outbox_not_the_real_one(env, academy_world):
    cli(env, DEV(env), triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade()])
    eval_outbox = env / "private" / "eval_outbox_dev-20261104.jsonl"
    assert [json.loads(l)["body"] for l in eval_outbox.read_text(encoding="utf-8").splitlines()] == [REPLY_BODY]
    assert not academy_world.outbox.exists()
    import os
    assert os.environ["ACADEMY_OUTBOX_FILE"] == str(academy_world.outbox)    # and the setting is put back afterwards


def test_a_failing_run_is_visible_in_the_table(env):
    """Triage misses the sensitive message and it gets drafted: the bars must show FAIL."""
    dataset = write(env / "dev.jsonl", [row_for("s1", "sensitive", [])])
    code, _, out = cli(env, dataset, triage_script=["fees_plans"], agent_script=drafting(), judge_script=[good_grade()])
    table = (out / "eval_dev-20261104.md").read_text(encoding="utf-8")
    assert code == 0 and table.count("**FAIL**") >= 2 and "missed: s1" in table and "reached it: s1" in table


def test_your_scores_are_compared_with_the_judge(env):
    write(env / "my_scores.jsonl", [{"id": "d1", "quality": 3}])
    _, _, out = cli(env, DEV(env), triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade(5)])
    agreement = json.loads((out / "eval_dev-20261104.json").read_text(encoding="utf-8"))["summary"]["agreement"]
    assert agreement["n"] == 1 and agreement["exact"] == 0.0 and agreement["bias"] == 2
    assert "Same score" in (out / "eval_dev-20261104.md").read_text(encoding="utf-8")


# ---- small batches, pauses, resume -----------------------------------------------------------------

def test_messages_go_in_batches_with_a_pause_between_them(env):
    sleeps = []
    code, _, _ = cli(env, DEV(env), "--batch-size", "1", triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(),
                     judge_script=[good_grade()], sleeps=sleeps)
    assert code == 0 and sleeps == [7.0, 7.0]       # three batches of one: two pauses of --pause seconds


def test_a_run_can_be_stopped_and_continued_and_scored_ids_are_skipped(env):
    dataset = DEV(env)
    code, output, out = cli(env, dataset, "--limit", "1", triage_script=["book_demo"], agent_script=drafting(), judge_script=[good_grade()])
    assert code == 2 and list(latest(out)) == ["d1"] and "Continue with the same run id" in output and "--run-id dev-20261104" in output
    # Second run: the fake triage model has only two answers. If d1 were run again it would run out of answers and fail.
    code, output, out = cli(env, dataset, triage_script=["sensitive", "ignore"])
    assert code == 0 and "1 of 3 messages are already scored; 2 to do" in output
    assert sorted(latest(out)) == ["d1", "d2", "d3"] and all(r["status"] == "scored" for r in latest(out).values())


def test_a_judge_that_hits_a_rate_limit_leaves_the_reply_waiting_and_the_next_run_judges_it_without_redoing_the_agent(env, monkeypatch):
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    dataset = write(env / "dev.jsonl", [row_for("d1", "book_demo", BOOK)])
    limited = [RuntimeError("429 Too Many Requests: rate limit")] * 2         # first try + one retry
    code, output, out = cli(env, dataset, triage_script=["book_demo"], agent_script=drafting(), judge_script=limited)
    assert code == 2 and "d1: failed (RuntimeError) - rate limit" in output and "waiting for the judge" in output
    assert saved_rows(out)[-1]["judge_status"] == "pending"
    # The agent and triage fakes below have NO answers: the agent stage must not run again.
    code, _, out = cli(env, dataset, judge_script=[good_grade(4)])
    assert code == 0 and saved_rows(out)[-1]["judge_status"] == "done" and saved_rows(out)[-1]["judge"]["quality"] == 4


def test_three_rate_limit_failures_in_a_row_stop_the_run(env, monkeypatch):
    monkeypatch.setenv("MODEL_MAX_RETRIES", "0")
    dataset = write(env / "dev.jsonl", [row_for(f"d{i}", "book_demo", BOOK) for i in range(1, 6)])
    code, output, out = cli(env, dataset, "--batch-size", "10", triage_script=["book_demo"] * 3, agent_script=drafting(3),
                            judge_script=[RuntimeError("429 rate limit")] * 3)
    assert code == 2 and "3 rate-limit failures in a row: stopping" in output
    assert [r["id"] for r in saved_rows(out)] == ["d1", "d2", "d3"]          # d4 and d5 were never started


def test_a_message_that_fails_is_recorded_and_retried_on_the_next_run(env):
    dataset = write(env / "dev.jsonl", [row_for("d1", "book_demo", BOOK), row_for("d2", "ignore", [])])
    code, output, out = cli(env, dataset, triage_script=["book_demo", "ignore"], agent_script=[ValueError(f"boom {SECRET_TEXT}")], judge_script=[])
    rows = {r["id"]: r for r in saved_rows(out)}
    assert code == 2 and rows["d1"]["status"] == "error" and rows["d1"]["error"] == "ValueError" and rows["d2"]["status"] == "scored"
    assert SECRET_TEXT not in (out / "eval_dev-20261104.jsonl").read_text(encoding="utf-8") + output
    code, _, out = cli(env, dataset, triage_script=["book_demo"], agent_script=drafting(), judge_script=[good_grade()])
    assert code == 0 and {r["id"]: r["status"] for r in saved_rows(out)} == {"d1": "scored", "d2": "scored"}


def test_the_judge_can_be_skipped_and_added_later(env):
    dataset = write(env / "dev.jsonl", [row_for("d1", "book_demo", BOOK)])
    code, output, out = cli(env, dataset, "--no-judge", triage_script=["book_demo"], agent_script=drafting())
    assert saved_rows(out)[-1]["judge_status"] == "skipped" and code == 0
    code, _, out = cli(env, dataset, judge_script=[good_grade()])
    assert code == 0 and saved_rows(out)[-1]["judge_status"] == "done"


def test_if_the_local_reply_file_is_deleted_the_agent_stage_is_redone_for_the_judge(env):
    dataset = write(env / "dev.jsonl", [row_for("d1", "book_demo", BOOK)])
    cli(env, dataset, "--no-judge", triage_script=["book_demo"], agent_script=drafting())
    (env / "private" / "eval_private_dev-20261104.json").unlink()
    code, _, out = cli(env, dataset, triage_script=["book_demo"], agent_script=drafting(), judge_script=[good_grade()])
    assert code == 0 and saved_rows(out)[-1]["judge_status"] == "done"


def test_report_only_calls_no_model(env):
    dataset = DEV(env)
    cli(env, dataset, triage_script=["book_demo", "sensitive", "ignore"], agent_script=drafting(), judge_script=[good_grade()])
    code, output, _ = cli(env, dataset, "--report-only")           # the fakes here have no answers at all
    assert code == 0 and "Pass bars" in output


# ---- the settings ----------------------------------------------------------------------------------

def test_the_judge_may_not_be_the_same_model_as_the_agent(env, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "fake:agent-big")
    code, output, _ = cli(env, DEV(env))
    assert code == 1 and "JUDGE_MODEL and AGENT_MODEL" in output


def test_a_missing_model_setting_names_the_variable(env, monkeypatch):
    monkeypatch.delenv("TRIAGE_MODEL")
    code, output, _ = cli(env, DEV(env))
    assert code == 1 and "TRIAGE_MODEL" in output


def test_a_missing_dataset_file_says_how_to_try_the_samples(env):
    code, output, _ = cli(env, env / "nope.jsonl")
    assert code == 1 and "does not exist" in output and "sample_messages.jsonl" in output


@pytest.mark.parametrize("extra", [["--force"], ["--batch-size", "0"], ["--limit", "0"], ["--pause", "-1"]])
def test_bad_options_are_refused(env, extra):
    assert cli(env, DEV(env), *extra)[0] == 1


# ---- the held-out rules ----------------------------------------------------------------------------

def heldout_file(env, n=2):
    return write(env / "heldout.jsonl", [row_for(f"h{i}", "book_demo", BOOK, split="heldout") for i in range(1, n + 1)])


def heldout_run(env, dataset, *extra, n=1, **kw):
    return cli(env, dataset, "--heldout", *extra, triage_script=["book_demo"] * n, agent_script=drafting(n), judge_script=[good_grade()] * n, **kw)


def test_held_out_messages_do_not_run_without_the_flag(env):
    code, output, _ = cli(env, heldout_file(env), triage_script=["book_demo"])
    assert code == 1 and "held-out" in output and "--heldout" in output


def test_the_flag_refuses_a_file_that_contains_dev_messages(env):
    mixed = write(env / "mixed.jsonl", [row_for("h1", "book_demo", BOOK, split="heldout"), row_for("d1", "book_demo", BOOK)])
    code, output, _ = cli(env, mixed, "--heldout")
    assert code == 1 and "only held-out messages" in output


def test_the_first_held_out_run_works_and_is_recorded(env):
    code, output, out = heldout_run(env, heldout_file(env, 1))
    assert code == 0 and "HELD-OUT RUN" in output
    lock = json.loads((out / "heldout_lock.json").read_text(encoding="utf-8"))
    assert [r["run_id"] for r in lock["runs"]] == ["heldout"] and lock["runs"][0]["forced"] is False
    assert "HELD-OUT run" in (out / "eval_heldout.md").read_text(encoding="utf-8")


def test_a_second_held_out_run_is_refused_and_calls_no_model(env):
    dataset = heldout_file(env, 1)
    heldout_run(env, dataset)
    shared_out = env / "results"
    said = []
    # no fake models are installed here: if the run started, building a model would fail loudly
    code = safety_eval.main(["--heldout", "--dataset", str(dataset), "--out-dir", str(shared_out), "--private-dir", str(env / "private")],
                            workflow=build_graph(), now=lambda: NOW, say=said.append)
    assert code == 1 and "already made on 2026-11-04" in "\n".join(said) and "--force" in "\n".join(said)
    assert len(json.loads((shared_out / "heldout_lock.json").read_text(encoding="utf-8"))["runs"]) == 1


def test_force_allows_a_second_run_under_a_new_id_and_marks_it(env):
    dataset = heldout_file(env, 1)
    heldout_run(env, dataset)
    code, _, out = heldout_run(env, dataset, "--force")
    lock = json.loads((out / "heldout_lock.json").read_text(encoding="utf-8"))
    assert code == 0 and len(lock["runs"]) == 2 and lock["runs"][1]["forced"] is True and lock["runs"][1]["run_id"].startswith("heldout-2026")
    forced_table = next(p for p in out.glob("eval_heldout-*.md")).read_text(encoding="utf-8")
    assert "Forced re-run" in forced_table
    assert "Forced re-run" not in (out / "eval_heldout.md").read_text(encoding="utf-8")


def test_an_unfinished_held_out_run_can_be_continued_but_a_finished_one_cannot(env):
    dataset = heldout_file(env, 2)
    code, _, out = heldout_run(env, dataset, "--limit", "1")
    assert code == 2
    code, output, out = cli(env, dataset, "--heldout", triage_script=["book_demo"], agent_script=drafting(), judge_script=[good_grade()])
    assert code == 0 and "1 of 2 messages are already scored" in output
    assert len(json.loads((out / "heldout_lock.json").read_text(encoding="utf-8"))["runs"]) == 1     # a resume is not a new run
    code, output, _ = cli(env, dataset, "--heldout")
    assert code == 1 and "already made" in output


def test_reading_the_held_out_numbers_again_needs_no_force(env):
    dataset = heldout_file(env, 1)
    heldout_run(env, dataset)
    code, output, _ = cli(env, dataset, "--heldout", "--report-only")
    assert code == 0 and "Pass bars" in output


def test_a_held_out_run_that_cannot_start_is_not_recorded(env, monkeypatch):
    monkeypatch.delenv("AGENT_MODEL")
    code, output, out = cli(env, heldout_file(env, 1), "--heldout")
    assert code == 1 and "AGENT_MODEL" in output and not (out / "heldout_lock.json").exists()


def test_force_is_only_for_held_out_runs(env):
    code, output, _ = cli(env, DEV(env), "--force")
    assert code == 1 and "--heldout" in output


def test_the_sample_rows_run_as_a_dev_run_and_the_table_says_they_are_samples(env):
    dataset = write(env / "samples.jsonl", [row_for("SAMPLE-01", "book_demo", BOOK, sample=True)])
    _, _, out = cli(env, dataset, triage_script=["book_demo"], agent_script=drafting(), judge_script=[good_grade()])
    assert "includes SAMPLE rows" in (out / "eval_dev-20261104.md").read_text(encoding="utf-8")
