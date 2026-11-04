"""Labelled parent messages: file format, loader and validator.

One JSON object per line (JSONL), UTF-8. Format and field meanings: `data/eval/README.md`.
All messages must be synthetic (hard rule 5).
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from email_assistant.academy.guards import SIGNAL_TOOLS
from email_assistant.academy.schemas import LABELS
from email_assistant.academy.tools import ACADEMY_TOOLS

SPLITS = ("dev", "heldout")
LANGUAGES = ("en", "te", "tenglish")
# Tool names a row may expect, lower case (the same way the test helpers compare them).
KNOWN_TOOLS = frozenset({t.name.lower() for t in ACADEMY_TOOLS}) | {n.lower() for n in SIGNAL_TOOLS}

REQUIRED_FIELDS = ("id", "split", "language", "text", "expected_label", "expected_tool_calls", "must_include", "must_not_say")
OPTIONAL_FIELDS = ("sender", "sample")


class DatasetError(ValueError):
    """The dataset file is not valid. The message says which line and what is wrong."""


@dataclass(frozen=True)
class Message:
    id: str
    split: str
    language: str
    text: str
    expected_label: str
    expected_tool_calls: list[str]
    must_include: list[str]
    must_not_say: list[str]
    sender: str = "Parent <parent@example.invalid>"
    sample: bool = False

    def graph_input(self) -> dict:
        """The dict the graph takes as `message_input`."""
        return {"from": self.sender, "text": self.text}


def _check_row(row: object, where: str) -> Message:
    if not isinstance(row, dict):
        raise DatasetError(f"{where}: each line must be a JSON object.")
    missing = [f for f in REQUIRED_FIELDS if f not in row]
    if missing:
        raise DatasetError(f"{where}: missing field(s): {', '.join(missing)}.")
    unknown = sorted(set(row) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS))
    if unknown:
        raise DatasetError(f"{where}: unknown field(s): {', '.join(unknown)}.")
    for name in ("id", "text"):
        if not isinstance(row[name], str) or not row[name].strip():
            raise DatasetError(f"{where}: {name!r} must be a non-empty string.")
    if row["split"] not in SPLITS:
        raise DatasetError(f"{where}: split {row['split']!r} must be one of {SPLITS}.")
    if row["language"] not in LANGUAGES:
        raise DatasetError(f"{where}: language {row['language']!r} must be one of {LANGUAGES}.")
    if row["expected_label"] not in LABELS:
        raise DatasetError(f"{where}: expected_label {row['expected_label']!r} must be one of {LABELS}.")
    for name in ("expected_tool_calls", "must_include", "must_not_say"):
        value = row[name]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise DatasetError(f"{where}: {name!r} must be a list of non-empty strings.")
    bad_tools = [t for t in row["expected_tool_calls"] if t not in KNOWN_TOOLS]
    if bad_tools:
        raise DatasetError(f"{where}: unknown tool(s) in expected_tool_calls: {bad_tools}. Use lower case: {sorted(KNOWN_TOOLS)}.")
    if row["expected_label"] in ("sensitive", "ignore") and row["expected_tool_calls"]:
        raise DatasetError(f"{where}: a {row['expected_label']!r} message must have expected_tool_calls = [].")
    if "sender" in row and not isinstance(row["sender"], str):
        raise DatasetError(f"{where}: 'sender' must be a string.")
    if "sample" in row and not isinstance(row["sample"], bool):
        raise DatasetError(f"{where}: 'sample' must be true or false.")
    return Message(**row)


def load_messages(path: str | Path) -> list[Message]:
    """Read and validate a JSONL file. Raises DatasetError on the first problem."""
    path = Path(path)
    messages: list[Message] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name} line {number}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{where}: not valid JSON ({exc.msg}).") from None
        message = _check_row(row, where)
        if message.id in seen:
            raise DatasetError(f"{where}: duplicate id {message.id!r}.")
        seen.add(message.id)
        messages.append(message)
    if not messages:
        raise DatasetError(f"{path.name}: no messages.")
    return messages


def check_full_set(messages: list[Message]) -> list[str]:
    """Problems with a finished dataset: 40 real messages, 30 dev + 10 heldout, 8+ Telugu/Tenglish.

    Returns a list of problems (empty means fine). Not used for the 3 samples.
    """
    problems = []
    if any(m.sample for m in messages):
        problems.append("contains SAMPLE rows; the finished set must have real messages only.")
    splits = Counter(m.split for m in messages)
    if len(messages) != 40:
        problems.append(f"expected 40 messages, found {len(messages)}.")
    if splits["dev"] != 30 or splits["heldout"] != 10:
        problems.append(f"expected 30 dev + 10 heldout, found {splits['dev']} dev + {splits['heldout']} heldout.")
    telugu = sum(1 for m in messages if m.language in ("te", "tenglish"))
    if telugu < 8:
        problems.append(f"expected at least 8 Telugu/Tenglish messages, found {telugu}.")
    return problems


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "data/eval/parent_messages.jsonl"
    rows = load_messages(target)
    print(f"{target}: {len(rows)} messages are valid.")
    if "--full" in sys.argv:
        issues = check_full_set(rows)
        print("\n".join(f"  PROBLEM: {p}" for p in issues) or "  full-set check passed (40 = 30 dev + 10 heldout, 8+ Telugu/Tenglish).")
        sys.exit(1 if issues else 0)
