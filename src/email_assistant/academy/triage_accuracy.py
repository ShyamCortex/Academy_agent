"""Measure how often triage picks the expected label. Run on your own keys:

    uv run python -m email_assistant.academy.triage_accuracy data/eval/sample_messages.jsonl

Prints accuracy overall and per label, and saves a dated JSON file in docs/agent/results/.
Rate limits (HTTP 429) are retried with exponential backoff by the model wrapper in
`email_assistant/models.py`; MODEL_MAX_RETRIES and friends in `.env` tune it.

The saved file holds ids and labels only: never message text (hard rule 5).
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from email_assistant.academy import config
from email_assistant.academy.dataset import Message, load_messages
from email_assistant.academy.schemas import LABELS


def run_triage(messages: list[Message], classify: Callable[[dict], str]) -> list[dict]:
    """Label every message. A message whose triage call keeps failing is recorded as an error."""
    outcomes = []
    for message in messages:
        try:
            predicted, error = classify(message.graph_input()), None
        except Exception as exc:  # noqa: BLE001 - keep the error type only, never its text
            predicted, error = None, type(exc).__name__
        outcomes.append({"id": message.id, "language": message.language, "expected": message.expected_label,
                         "predicted": predicted, "correct": predicted == message.expected_label, "error": error})
    return outcomes


def summarize(outcomes: list[dict]) -> dict:
    """Accuracy overall and per expected label, plus which labels got confused."""
    total = len(outcomes)
    correct = sum(o["correct"] for o in outcomes)
    per_label = {}
    for label in LABELS:
        rows = [o for o in outcomes if o["expected"] == label]
        if rows:
            per_label[label] = {"n": len(rows), "correct": sum(o["correct"] for o in rows),
                                "accuracy": sum(o["correct"] for o in rows) / len(rows)}
    confusions = Counter((o["expected"], o["predicted"]) for o in outcomes if not o["correct"] and o["error"] is None)
    return {
        "total": total, "correct": correct, "accuracy": correct / total if total else 0.0,
        "errors": sum(o["error"] is not None for o in outcomes),
        "per_label": per_label,
        "confusions": [{"expected": e, "predicted": p, "count": c} for (e, p), c in confusions.most_common()],
    }


def save_results(summary: dict, outcomes: list[dict], dataset: str, out_dir: Path, now: datetime | None = None) -> Path:
    """Write a dated JSON file and return its path."""
    now = now or datetime.now(config.TIMEZONE)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"triage_accuracy_{now.strftime('%Y%m%d_%H%M%S')}.json"
    record = {"date": now.isoformat(timespec="seconds"), "dataset": dataset,
              "triage_model": os.environ.get("TRIAGE_MODEL", ""), "summary": summary, "outcomes": outcomes}
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def format_report(summary: dict) -> str:
    lines = [f"Triage accuracy: {summary['correct']}/{summary['total']} = {summary['accuracy']:.0%}"
             + (f"  ({summary['errors']} call(s) failed and count as wrong)" if summary["errors"] else ""),
             "", f"{'label':<22}{'correct':>8}{'of':>5}{'accuracy':>10}"]
    for label, row in summary["per_label"].items():
        lines.append(f"{label:<22}{row['correct']:>8}{row['n']:>5}{row['accuracy']:>10.0%}")
    if summary["confusions"]:
        lines += ["", "Mix-ups (expected -> predicted):"]
        lines += [f"  {c['expected']} -> {c['predicted']}: {c['count']}" for c in summary["confusions"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, classify: Callable[[dict], str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a labelled dataset through triage and report accuracy.")
    parser.add_argument("dataset", help="JSONL file, e.g. data/eval/sample_messages.jsonl")
    parser.add_argument("--out-dir", default="docs/agent/results", help="where the dated result file goes")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(".env")
    from email_assistant.models import require_distinct_judge
    require_distinct_judge()  # every eval entry point refuses a judge that equals the drafter

    messages = load_messages(args.dataset)
    if classify is None:
        from email_assistant.academy.graph import triage_message as classify
        from email_assistant.models import model_spec
        model_spec("triage")  # stop with a clear error if TRIAGE_MODEL is missing, before any call
    outcomes = run_triage(messages, classify)
    summary = summarize(outcomes)
    print(format_report(summary))
    print(f"\nSaved: {save_results(summary, outcomes, args.dataset, Path(args.out_dir))}")
    if summary["errors"] == summary["total"]:
        print("Every triage call failed, so this is not a real accuracy. Check your keys and TRIAGE_MODEL.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
