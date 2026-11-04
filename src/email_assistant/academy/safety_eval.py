"""Safety evals over a labelled dataset file. Run on your laptop with your own keys.

    uv run python -m email_assistant.academy.safety_eval                       dev run: data/eval/dev_messages.jsonl (30 messages)
    uv run python -m email_assistant.academy.safety_eval --dataset data/eval/sample_messages.jsonl      the 4 sample rows
    uv run python -m email_assistant.academy.safety_eval --heldout             the 10 held-out messages, ONCE, at the very end

What one run does for each message: triage -> (if drafted) the agent with a scripted owner who accepts -> code
scorers (`eval_scorers.py`) -> the judge (`judge.py`). Results go to docs/agent/results/:

    eval_<run-id>.jsonl   one row per message (ids, labels, booleans, numbers; never message text). The resume file.
    eval_<run-id>.json    the summary numbers
    eval_<run-id>.md      the results table

Free tiers allow few requests, so a run is made to be stopped and continued:
- messages go in small batches (--batch-size, default 5) with a pause between them (--pause, default 20 s);
- every model call retries on rate limits with exponential backoff (`models.py`);
- after each message its row is saved; run the same command again and ids that are already scored are skipped;
- three messages in a row failing on rate limits stops the run (exit code 2) and prints the resume command.

Calendar: every message runs against its own in-memory FakeCalendar, the same every run, so a run never touches Google
and uses no Google quota. A message with an `existing_demo` field has that booking put in first (see data/eval/README.md).
--real-calendar uses the real Google test calendar instead (it needs the Google setup, writes real events, and seeds
nothing). Each result row and the results table record which one was used, and a run cannot be continued with the other.

Held-out rules:
- held-out messages run only with --heldout, and dev messages never run with it (so the 10 stay unseen);
- the first --heldout run is recorded in docs/agent/results/heldout_lock.json; another run is refused unless --force;
- the only way to continue is to resume the same unfinished run (same --run-id).
Do not change prompts because of held-out numbers.

Files kept only on your computer (git-ignored, in outbox/): eval_private_<run-id>.json (the drafted replies and tool
results, so a judge that hit a rate limit can be run later without redoing the agent) and eval_review_<run-id>.md
(a readable page to check replies and write data/eval/my_scores.jsonl).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
from unittest import mock

from email_assistant.academy import config, settings
from email_assistant.academy.dataset import Message, load_messages
from email_assistant.academy.eval_harness import UsageTracker, run_message
from email_assistant.academy.eval_scorers import error_row, score_trace, summarize
from email_assistant.academy.judge import MY_SCORES_FILE, JudgeFn, agreement, judge_reply, load_my_scores

RESULTS_DIR = Path("docs/agent/results")
PRIVATE_DIR = Path("outbox")  # git-ignored
DEV_FILE = Path("data/eval/dev_messages.jsonl")
HELDOUT_FILE = Path("data/eval/heldout_messages.jsonl")
STOP_AFTER_RATE_LIMITS = 3


class EvalRefused(RuntimeError):
    """The run will not start. The message says why and what to do."""


# ---- files -----------------------------------------------------------------------------------------

def rows_path(out_dir: Path, run_id: str) -> Path:
    return out_dir / f"eval_{run_id}.jsonl"


def load_rows(path: Path) -> dict[str, dict]:
    """The latest row for each message id (a later line replaces an earlier one for the same id)."""
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                latest[row["id"]] = row
    return latest


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class PrivateStore:
    """The drafted replies, tool results and judge notes of one run. Local only: this holds generated text."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def put(self, message_id: str, **fields) -> None:
        self.data.setdefault(message_id, {}).update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, message_id: str) -> dict | None:
        return self.data.get(message_id)


# ---- the held-out guard ----------------------------------------------------------------------------

def choose_dataset(args: argparse.Namespace) -> Path:
    return Path(args.dataset) if args.dataset else (HELDOUT_FILE if args.heldout else DEV_FILE)


def check_split(messages: Sequence[Message], heldout: bool) -> None:
    """--heldout takes only held-out rows; a normal run takes none. This keeps the 10 unseen messages unseen."""
    held = [m for m in messages if m.split == "heldout"]
    if heldout and len(held) != len(messages):
        raise EvalRefused("--heldout needs a file with only held-out messages (split = heldout). Dev messages must not be in a held-out run.")
    if not heldout and held:
        raise EvalRefused(f"This file has {len(held)} held-out message(s). They run only with --heldout, once, at the end. Use the dev file.")


def read_lock(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"runs": []}


def check_heldout_lock(lock_path: Path, run_id: str, *, force: bool, unfinished: bool, now: datetime, n_messages: int) -> None:
    """Refuse a held-out run unless it is the first, a resume of the unfinished run, or forced. Records the run."""
    lock = read_lock(lock_path)
    runs = lock["runs"]
    resuming = bool(runs) and runs[-1]["run_id"] == run_id and unfinished
    if runs and not resuming and not force:
        first = runs[0]
        raise EvalRefused(
            f"A held-out run was already made on {first['date'][:10]} (run id {first['run_id']}). The held-out messages are meant to be "
            f"looked at once. Reading the numbers again is fine (they are in {RESULTS_DIR}). Running again needs --force, and then the "
            "set is no longer unseen: say so wherever you quote the number."
        )
    if not resuming:
        runs.append({"run_id": run_id, "date": now.isoformat(timespec="seconds"), "messages": n_messages, "forced": bool(runs)})
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


# ---- the run ---------------------------------------------------------------------------------------

def calendar_mode(real_calendar: bool) -> str:
    return "real" if real_calendar else "fake"


def check_real_calendar_settings() -> None:
    """--real-calendar needs the Google test calendar set up. Say what is missing instead of failing in the middle of a run."""
    from email_assistant.academy import settings
    from email_assistant.academy.calendar_backend import token_path

    if not os.environ.get(settings.CALENDAR_ID_VAR, "").strip():
        raise EvalRefused(f"--real-calendar needs {settings.CALENDAR_ID_VAR} (the TEST calendar's id) in .env. See docs/agent/LAPTOP_SETUP.md section B.")
    if not token_path().exists():
        raise EvalRefused(f"--real-calendar needs the saved Google sign-in {token_path()}. Run: uv run python -m email_assistant.academy.google_auth")


def needs_judge(row: dict | None, judging: bool) -> bool:
    return bool(judging and row and row["status"] == "scored" and row["judge_status"] in ("pending", "skipped"))


def needs_work(row: dict | None, judging: bool) -> bool:
    return row is None or row["status"] != "scored" or needs_judge(row, judging)


def judge_one(message: Message, row: dict, store: PrivateStore, judge: JudgeFn) -> dict:
    """Grade a drafted reply and return the updated row. Raises if the judge call fails after its retries."""
    saved = store.get(message.id)
    tracker = UsageTracker()
    started = time.perf_counter()
    grade = judge(message, saved["drafts"], [tuple(o) for o in saved["outputs"]], {"callbacks": [tracker], "metadata": {"message_id": message.id}})
    seconds = time.perf_counter() - started
    store.put(message.id, judge_reasoning=grade.reasoning)
    return {**row, "judge_status": "done", "judge_seconds": round(seconds, 3), "judge_usage": tracker.snapshot(),
            "judge": {"quality": grade.quality, "must_include_met": grade.must_include_met,
                      "must_not_say_violated": grade.must_not_say_violated, "gives_chess_advice": grade.gives_chess_advice,
                      "dates_match_tools": grade.dates_match_tools}}


def run_eval(
    messages: Sequence[Message], *, run_id: str, out_dir: Path, private_dir: Path, workflow=None, judging: bool = True,
    judge: JudgeFn = judge_reply, batch_size: int = 5, pause: float = 20.0, limit: int | None = None,
    real_calendar: bool = False, sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print,
) -> tuple[list[dict], bool]:
    """Score every message that still needs work, in batches. Returns (all rows, stopped_early)."""
    from email_assistant.academy.graph import build_graph
    from email_assistant.models import describe_error, is_retryable

    workflow = workflow or build_graph()
    path = rows_path(out_dir, run_id)
    done = load_rows(path)
    store = PrivateStore(private_dir / f"eval_private_{run_id}.json")
    todo = [m for m in messages if needs_work(done.get(m.id), judging)]
    say(f"{len(messages) - len(todo)} of {len(messages)} messages are already scored; {len(todo)} to do" + (f" (limit {limit})." if limit else "."))
    if limit is not None:
        todo = todo[:limit]

    rate_limited_in_a_row = 0
    for start in range(0, len(todo), batch_size):
        if start:
            say(f"  pause {pause:g}s before the next batch of {batch_size} (free-tier limits) ...")
            sleep(pause)
        for message in todo[start:start + batch_size]:
            row = done.get(message.id)
            stage = "agent"
            try:
                lost_replies = needs_judge(row, judging) and store.get(message.id) is None  # local file was deleted: redo the agent stage
                if row is None or row["status"] != "scored" or lost_replies:
                    trace = run_message(workflow, message, run_id=run_id, real_calendar=real_calendar)
                    row = score_trace(message, trace)
                    row["calendar"] = calendar_mode(real_calendar)
                    store.put(message.id, drafts=trace.drafts(), outputs=[list(o) for o in trace.outputs])
                    if row["judge_status"] == "pending" and not judging:
                        row["judge_status"] = "skipped"
                    append_row(path, row)
                    done[message.id] = row
                if needs_judge(row, judging):
                    stage = "judge"
                    row = judge_one(message, row, store, judge)
                    append_row(path, row)
                    done[message.id] = row
                rate_limited_in_a_row = 0
                say(f"  {message.id}: ok")
            except Exception as exc:  # noqa: BLE001 - only the type is kept; the text may quote the message
                limited = is_retryable(exc)
                rate_limited_in_a_row = rate_limited_in_a_row + 1 if limited else 0
                if row is None or row["status"] != "scored":
                    row = {**error_row(message, exc), "calendar": calendar_mode(real_calendar)}
                    append_row(path, row)
                    done[message.id] = row
                # Type and provider code only (e.g. "BadRequestError, code tool_use_failed"): the text may quote the message.
                say(f"  {message.id}: {stage} failed ({describe_error(exc)}){' - rate limit' if limited else ''}")
                if rate_limited_in_a_row >= STOP_AFTER_RATE_LIMITS:
                    say(f"  {STOP_AFTER_RATE_LIMITS} rate-limit failures in a row: stopping. Run the same command later to continue.")
                    return [done[m.id] for m in messages if m.id in done], True
    return [done[m.id] for m in messages if m.id in done], False


# ---- the results table -----------------------------------------------------------------------------

def pct(value: float | None, digits: int = 0) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(summary: dict, meta: dict) -> str:
    """The results table, in markdown."""
    t, s, n, tl, q, a = summary["triage"], summary["sensitive"], summary["numbers"], summary["tools"], summary["quality"], summary["agreement"]
    title = "HELD-OUT run" if meta["heldout"] else "Dev run"
    lines = [
        f"# Safety eval: {title} `{meta['run_id']}`", "",
        f"- Date: {meta['date']} (India time)",
        f"- Dataset: `{meta['dataset']}`: {summary['n_messages']} messages, {summary['n_scored']} scored, {summary['n_errors']} failed to run"
        + (" **(includes SAMPLE rows, not the real 40)**" if meta["has_samples"] else ""),
        f"- Models: triage `{meta['models']['triage']}`, agent `{meta['models']['agent']}`, judge `{meta['models']['judge']}`",
        ("- Calendar: **REAL Google test calendar** (real events were written; no existing bookings were seeded)" if meta["calendar"] == "real"
         else "- Calendar: fake in-memory calendar, empty except for each message's `existing_demo` booking; the same on every run, no Google used"),
    ]
    if meta["heldout"]:
        lines.append("- Held-out set: look at these numbers once. Do not change prompts because of them." + (" **Forced re-run: the set is no longer unseen.**" if meta.get("forced") else ""))
    if q["n_waiting"]:
        lines.append(f"- **{q['n_waiting']} reply(ies) are still waiting for the judge.** Run the same command again.")
    if summary["n_errors"]:
        lines.append(f"- **{summary['n_errors']} message(s) failed to run.** They count as wrong below. Run the same command again to retry them.")

    lines += ["", "## Pass bars", "", "| Check | Bar | Result | |", "|---|---|---|---|"]
    names = {"sensitive_recall": "Sensitive recall (label = sensitive)", "sensitive_never_drafted": "Sensitive never reaches the drafter",
             "numbers_grounded": "Drafts with every number in the tool outputs and every weekday right", "no_chess_teaching": "Replies with no chess teaching (judge)"}
    for target in summary["targets"]:
        mark = "not measured" if target["met"] is None else ("PASS" if target["met"] else "**FAIL**")
        lines.append(f"| {names[target['name']]} | {pct(target['target'])} | {pct(target['value'])} | {mark} |")

    lines += ["", "## Triage accuracy", "", f"Overall: **{t['correct']}/{t['total']} = {pct(t['accuracy'])}**", "",
              "| Label | Correct | Of | Accuracy |", "|---|---:|---:|---:|"]
    lines += [f"| {label} | {r['correct']} | {r['n']} | {pct(r['accuracy'])} |" for label, r in t["per_label"].items()]
    lines += ["", "| Language | Correct | Of | Accuracy |", "|---|---:|---:|---:|"]
    lines += [f"| {lang} | {r['correct']} | {r['n']} | {pct(r['accuracy'])} |" for lang, r in t["per_language"].items()]
    if t["confusions"]:
        lines += ["", "Mix-ups (expected -> predicted): " + "; ".join(f"{c['expected']} -> {c['predicted']} x{c['count']}" for c in t["confusions"])]

    lines += ["", "## Sensitive messages", "",
              f"- Expected sensitive: {s['n']}. Recall: **{pct(s['recall'])}**" + (f" (missed: {', '.join(s['missed_ids'])})" if s["missed_ids"] else ""),
              f"- Never reached the drafter: **{pct(s['never_drafted'])}**" + (f" (reached it: {', '.join(s['drafted_ids'])})" if s["drafted_ids"] else ""),
              f"- Precision: {pct(s['precision'])}" + (f" (sent to the owner without need: {', '.join(s['over_escalated_ids'])})" if s["over_escalated_ids"] else "")]

    lines += ["", "## Invented numbers", "",
              f"- Drafts checked: {n['drafts_checked']}. Clean: **{n['clean']}** ({pct(n['rate'])})",
              "- A draft is clean when every number, date and time in it appears in that run's tool outputs, and every weekday written next to a date is that date's weekday (`grounding.py`)."]
    lines += [f"- {o['id']}: " + "; ".join(part for part in (
        f"not in any tool output: {', '.join(o['numbers'])}" if o["numbers"] else "",
        f"weekday does not match its date: {', '.join(o['weekdays'])}" if o["weekdays"] else "") if part) for o in n["offenders"]]

    lines += ["", "## Tool calls", "",
              f"- Correct end to end: **{tl['passed']}/{tl['n']} = {pct(tl['rate'])}**; when triage was right: {pct(tl['rate_given_correct_triage'])} of {tl['n_correct_triage']}",
              f"- Missing tools: {tl['missing'] or 'none'}; unexpected writes: {tl['unexpected_writes'] or 'none'}",
              f"- Booked a time find_demo_slots did not return: {tl['bad_slot_ids'] or 'none'}; reply or booking to the wrong address: {tl['wrong_recipient_ids'] or 'none'}",
              f"- Messages where a tool answered with an error: {tl['messages_with_tool_errors']}"]
    if tl["failed_ids"]:
        lines.append(f"- Failed: {', '.join(tl['failed_ids'])}")

    lines += ["", "## Reply quality (judge)", "",
              f"- Judged: {q['n_judged']}. Mean score: **{num(q['mean'])}** of 5. Good enough (4 or 5): {pct(q['good_enough'])}",
              "- Scores: " + ", ".join(f"{k}: {v}" for k, v in q["distribution"].items()),
              f"- must_include met: {pct(q['must_include_met'])}; must_not_say broken: {q['must_not_say_violated_ids'] or 'none'}; chess advice: {q['chess_advice_ids'] or 'none'}",
              f"- Judge says a weekday or date does not match the tool results: {q['dates_mismatch_ids'] or 'none'}"]

    lines += ["", "## Judge vs your scores", ""]
    if a is None:
        lines.append(f"No overlap yet: put your 1-5 scores in `{meta['my_scores']}` (format in `data/eval/README.md`).")
    else:
        lines += [f"- Messages scored by both: {a['n']}. Same score: **{pct(a['exact'])}**. Within 1 point: {pct(a['within_one'])}",
                  f"- Judge minus you, on average: {num(a['bias'])} (above 0 = the judge is kinder than you)",
                  f"- Agree on \"good enough to send\": {pct(a['good_enough_agreement'])}; Cohen's kappa: {num(a['good_enough_kappa'])}",
                  f"- Differ by 2 or more: {[(d['id'], d['judge'], d['mine']) for d in a['disagreements']] or 'none'}"]

    u, lat = summary["usage"], summary["latency"]
    lines += ["", "## Cost and latency", "", "| Role | Model calls | Input tokens | Output tokens | Cost at your prices |", "|---|---:|---:|---:|---:|"]
    for role, counts in u["per_role"].items():
        lines.append(f"| {role} | {counts['calls']} | {counts['input_tokens']} | {counts['output_tokens']} | "
                     + (f"${counts['usd']:.4f}" if "usd" in counts else "n/a") + " |")
    lines += ["", f"- Tokens per message: {u['tokens_per_message']:.0f}. Total cost: " + (f"${u['total_usd']:.4f} (${u['usd_per_message']:.5f} per message)" if u["total_usd"] is not None else
              "not computed: the free tier costs nothing; set `EVAL_PRICE_<ROLE>_IN` / `_OUT` (US dollars per million tokens) in `.env` to see what these tokens would cost"),
              f"- Time per message (triage + agent, scripted owner): mean {num(lat['run_seconds']['mean'], 1)} s, p50 {num(lat['run_seconds']['p50'], 1)} s, "
              f"p95 {num(lat['run_seconds']['p95'], 1)} s, max {num(lat['run_seconds']['max'], 1)} s",
              f"- Judge call: mean {num(lat['judge_seconds']['mean'], 1)} s, p95 {num(lat['judge_seconds']['p95'], 1)} s",
              "- These times include waiting for rate-limit retries, so on a free tier they show the limit, not the model's speed."]
    return "\n".join(lines) + "\n"


def write_review(path: Path, messages: Sequence[Message], rows: dict[str, dict], store: PrivateStore) -> None:
    """A readable page to check replies and write my_scores.jsonl. Local only (it holds the replies)."""
    parts = ["# Replies to review (local file; do not commit)\n\nWrite your score for each reply in `data/eval/my_scores.jsonl`: "
             '`{"id": "...", "quality": 1-5}`. Score the reply before you read the judge\'s note.\n']
    for message in messages:
        row, saved = rows.get(message.id), store.get(message.id)
        if not row or row["status"] != "scored" or not saved or not saved.get("drafts"):
            continue
        reply = "\n\n".join(f"Subject: {d.get('subject', '')}\n{d.get('body', '')}" for d in saved["drafts"])
        judge = row.get("judge")
        parts.append(f"\n---\n## {message.id} ({message.language}, expected `{message.expected_label}`, triage said `{row['predicted_label']}`)\n"
                     f"**Parent wrote:**\n\n> {message.text}\n\n**Reply:**\n\n{reply}\n\n"
                     + (f"*Judge: {judge['quality']}/5. {saved.get('judge_reasoning', '')}*\n" if judge else "*Not judged yet.*\n"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


# ---- the command line ------------------------------------------------------------------------------

def default_run_id(heldout: bool, forced_again: bool, now: datetime) -> str:
    if heldout:
        return f"heldout-{now.strftime('%Y%m%d_%H%M%S')}" if forced_again else "heldout"
    return f"dev-{now.strftime('%Y%m%d')}"


def main(argv: Sequence[str] | None = None, *, workflow=None, judge: JudgeFn = judge_reply, sleep: Callable[[float], None] = time.sleep,
         now: Callable[[], datetime] | None = None, say: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="Safety evals over a labelled dataset file.")
    parser.add_argument("--dataset", help=f"JSONL file (default {DEV_FILE}, or {HELDOUT_FILE} with --heldout)")
    parser.add_argument("--heldout", action="store_true", help="run the held-out messages (once; a second run needs --force)")
    parser.add_argument("--force", action="store_true", help="allow a second held-out run (the set is then no longer unseen)")
    parser.add_argument("--run-id", help="name of the run; use the same id to continue it (default: dev-<date>, or heldout)")
    parser.add_argument("--batch-size", type=int, default=5, help="messages per batch (default 5)")
    parser.add_argument("--pause", type=float, default=20.0, help="seconds to wait between batches (default 20)")
    parser.add_argument("--limit", type=int, help="score at most this many messages now (a small trial run)")
    parser.add_argument("--real-calendar", action="store_true", help="use the real Google test calendar instead of the fake one (writes real events; seeds nothing)")
    parser.add_argument("--no-judge", action="store_true", help="skip the judge (code scorers only); run again later without this flag to add it")
    parser.add_argument("--report-only", action="store_true", help="call no model; rebuild the summary and table from the saved rows")
    parser.add_argument("--my-scores", default=str(MY_SCORES_FILE), help="your scores for the judge-agreement number")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR), help="where dated results go")
    parser.add_argument("--private-dir", default=str(PRIVATE_DIR), help="where the local-only replies go (git-ignored)")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(".env")
    clock = now or (lambda: datetime.now(config.TIMEZONE))
    out_dir, private_dir = Path(args.out_dir), Path(args.private_dir)
    judging = not args.no_judge
    try:
        if args.force and not args.heldout:
            raise EvalRefused("--force only applies to --heldout.")
        if args.batch_size < 1 or args.pause < 0 or (args.limit is not None and args.limit < 1):
            raise EvalRefused("--batch-size and --limit must be at least 1, and --pause cannot be negative.")
        dataset = choose_dataset(args)
        if not dataset.exists():
            raise EvalRefused(f"{dataset} does not exist yet. Write it (format: data/eval/README.md), or try the samples: --dataset data/eval/sample_messages.jsonl")
        messages = load_messages(dataset)
        check_split(messages, args.heldout)
        started = clock()
        lock_path = out_dir / "heldout_lock.json"
        again = args.heldout and bool(read_lock(lock_path)["runs"]) and not args.run_id
        run_id = args.run_id or default_run_id(args.heldout, again and args.force, started)
        existing = load_rows(rows_path(out_dir, run_id))
        unfinished = any(needs_work(existing.get(m.id), judging) for m in messages)
        mode = calendar_mode(args.real_calendar)
        other = {r.get("calendar", "fake") for r in existing.values()} - {mode}
        if other:
            raise EvalRefused(f"Run {run_id} was started with the {other.pop()} calendar, so it cannot be continued with the {mode} one. Use a new --run-id.")
        if args.real_calendar and not args.report_only:
            check_real_calendar_settings()

        if not args.report_only:
            # Check the model settings first: a held-out run must not be recorded if it cannot even start.
            from email_assistant.models import model_spec, require_distinct_judge

            for role in ("triage", "agent") + (("judge",) if judging else ()):
                model_spec(role)
            if judging:
                require_distinct_judge()
            if args.heldout:
                check_heldout_lock(lock_path, run_id, force=args.force, unfinished=unfinished, now=started, n_messages=len(messages))
                say("HELD-OUT RUN: look at these numbers once, and do not change prompts because of them.")
    except EvalRefused as exc:
        say(f"Refused: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a missing model setting or a bad dataset: say what, keep going no further
        say(f"Cannot start: {exc}")
        return 1

    if args.report_only:
        rows, stopped = [existing[m.id] for m in messages if m.id in existing], False
    else:
        if args.real_calendar:
            say("REAL CALENDAR: bookings the agent makes (the scripted owner accepts them) are written to the Google test calendar. No existing bookings are seeded.")
        # An eval must not touch the real outbox: approved replies go to an eval-only file.
        outbox = private_dir / f"eval_outbox_{run_id}.jsonl"
        with mock.patch.dict(os.environ, {settings.OUTBOX_FILE_VAR: str(outbox)}):
            rows, stopped = run_eval(messages, run_id=run_id, out_dir=out_dir, private_dir=private_dir, workflow=workflow, judging=judging,
                                     judge=judge, batch_size=args.batch_size, pause=args.pause, limit=args.limit,
                                     real_calendar=args.real_calendar, sleep=sleep, say=say)

    by_id = {r["id"]: r for r in rows}
    missing = [m for m in messages if m.id not in by_id]
    rows = [by_id[m.id] for m in messages if m.id in by_id]
    if not rows:
        say("Nothing is scored yet, so there is no table.")
        return 2 if stopped else 1
    mine = load_my_scores(args.my_scores)
    judge_scores = {r["id"]: r["judge"]["quality"] for r in rows if r["status"] == "scored" and r["judge_status"] == "done"}
    summary = summarize(rows, agreement=agreement(judge_scores, mine))
    runs = read_lock(out_dir / "heldout_lock.json")["runs"] if args.heldout else []
    meta = {"run_id": run_id, "heldout": args.heldout, "date": clock().strftime("%Y-%m-%d %H:%M"), "dataset": str(dataset),
            "has_samples": any(r.get("sample") for r in rows), "calendar": mode, "my_scores": args.my_scores, "forced": len(runs) > 1,
            "models": {role: os.environ.get(var, "") for role, var in (("triage", "TRIAGE_MODEL"), ("agent", "AGENT_MODEL"), ("judge", "JUDGE_MODEL"))}}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"eval_{run_id}.json").write_text(json.dumps({"meta": meta, "summary": summary}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    table = render_markdown(summary, meta)
    (out_dir / f"eval_{run_id}.md").write_text(table, encoding="utf-8")
    write_review(private_dir / f"eval_review_{run_id}.md", messages, by_id, PrivateStore(private_dir / f"eval_private_{run_id}.json"))
    say("\n" + table)
    say(f"Saved: {out_dir / f'eval_{run_id}.md'} (table), .json (numbers), .jsonl (per-message rows).")
    say(f"Replies to check by eye (local only): {private_dir / f'eval_review_{run_id}.md'}")

    if stopped or missing or summary["quality"]["n_waiting"] and judging or summary["n_errors"]:
        left = len(missing) + summary["n_errors"] + (summary["quality"]["n_waiting"] if judging else 0)
        say(f"\nNot finished ({left} item(s) left). Continue with the same run id:\n  uv run python -m email_assistant.academy.safety_eval "
            + " ".join(([f"--dataset {dataset}"] if args.dataset else []) + (["--heldout"] if args.heldout else []) + [f"--run-id {run_id}"] + (["--no-judge"] if args.no_judge else [])))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
