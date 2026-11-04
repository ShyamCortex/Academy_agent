"""The labelled-message file format: the loader accepts the samples and rejects bad rows."""

import copy
import json
from pathlib import Path

import pytest

from email_assistant.academy.dataset import DatasetError, Message, check_full_set, load_messages
from email_assistant.academy.schemas import LABELS

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "data" / "eval" / "sample_messages.jsonl"

GOOD = {"id": "t-1", "split": "dev", "language": "en", "text": "Hello", "expected_label": "book_demo",
        "expected_tool_calls": ["find_demo_slots", "send_reply"], "must_include": ["a slot"], "must_not_say": ["a fee"]}


def _write(tmp_path, *rows):
    path = tmp_path / "m.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def test_the_three_samples_are_valid_and_marked_sample():
    rows = load_messages(SAMPLES)
    assert [r.id for r in rows] == ["SAMPLE-01", "SAMPLE-02", "SAMPLE-03"]
    assert all(r.sample for r in rows)
    assert {r.expected_label for r in rows} <= set(LABELS)
    assert "sensitive" in {r.expected_label for r in rows}
    assert "tenglish" in {r.language for r in rows}


def test_a_row_becomes_the_graph_input():
    row = load_messages(SAMPLES)[0]
    assert row.graph_input() == {"from": row.sender, "text": row.text}


def test_unicode_telugu_text_survives(tmp_path):
    telugu = "నమస్తే, డెమో క్లాస్ ఎప్పుడు?"
    row = {**GOOD, "language": "te", "text": telugu}
    assert load_messages(_write(tmp_path, row))[0].text == telugu


def test_optional_fields_have_defaults(tmp_path):
    row = load_messages(_write(tmp_path, GOOD))[0]
    assert row.sample is False and row.sender.endswith("@example.invalid>")


def _without(field):
    row = copy.deepcopy(GOOD)
    del row[field]
    return row


BAD_ROWS = {
    "missing field": (_without("must_not_say"), "line 1: missing field"),
    "unknown field": ({**GOOD, "extra": 1}, "unknown field"),
    "old upstream label": ({**GOOD, "expected_label": "respond"}, "expected_label"),
    "bad split": ({**GOOD, "split": "test"}, "split"),
    "bad language": ({**GOOD, "language": "hindi"}, "language"),
    "empty text": ({**GOOD, "text": "  "}, "'text'"),
    "unknown tool": ({**GOOD, "expected_tool_calls": ["send_email"]}, "unknown tool"),
    "tool not lower case": ({**GOOD, "expected_tool_calls": ["Send_Reply"]}, "unknown tool"),
    "must_include not a list": ({**GOOD, "must_include": "a slot"}, "must_include"),
    "empty string in list": ({**GOOD, "must_not_say": [""]}, "must_not_say"),
    "sensitive with tools": ({**GOOD, "expected_label": "sensitive"}, "sensitive"),
    "ignore with tools": ({**GOOD, "expected_label": "ignore"}, "ignore"),
    "sample not a bool": ({**GOOD, "sample": "yes"}, "sample"),
    "not an object": ("[1, 2]", "JSON object"),
    "not json": ("{oops", "not valid JSON"),
}


@pytest.mark.parametrize("row,expected", BAD_ROWS.values(), ids=BAD_ROWS.keys())
def test_bad_rows_are_rejected_with_a_clear_message(tmp_path, row, expected):
    with pytest.raises(DatasetError, match=expected):
        load_messages(_write(tmp_path, row))


def test_the_error_names_the_line(tmp_path):
    with pytest.raises(DatasetError, match="line 3"):
        load_messages(_write(tmp_path, GOOD, {**GOOD, "id": "t-2"}, {**GOOD, "id": "t-3", "split": "nope"}))


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(DatasetError, match="duplicate id 't-1'"):
        load_messages(_write(tmp_path, GOOD, GOOD))


def test_an_empty_file_is_rejected(tmp_path):
    with pytest.raises(DatasetError, match="no messages"):
        load_messages(_write(tmp_path))


def _forty(**changes):
    rows = []
    for i in range(40):
        rows.append(Message(id=f"m-{i}", split="dev" if i < 30 else "heldout", language="tenglish" if i < 8 else "en",
                            text="x", expected_label="schedule", expected_tool_calls=["get_batches"],
                            must_include=["a"], must_not_say=["b"]))
    return rows


def test_a_finished_set_of_forty_passes_the_full_check():
    assert check_full_set(_forty()) == []


def test_the_full_check_catches_wrong_counts_and_samples():
    assert check_full_set(load_messages(SAMPLES))  # 3 samples are not a finished set
    short = _forty()[:39]
    assert any("40" in p for p in check_full_set(short))
    wrong_split = [Message(**{**m.__dict__, "split": "dev"}) for m in _forty()]
    assert any("30 dev + 10 heldout" in p for p in check_full_set(wrong_split))
    no_telugu = [Message(**{**m.__dict__, "language": "en"}) for m in _forty()]
    assert any("Telugu" in p for p in check_full_set(no_telugu))
