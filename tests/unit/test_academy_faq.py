"""The FAQ file: its format, its facts (only from config.py, matching the seeded SQL) and the recall@k questions."""

import re
from pathlib import Path

import pytest

from email_assistant.academy import config
from email_assistant.academy.faq import FaqError, load_faq, parse_faq
from email_assistant.academy.grounding import ungrounded
from email_assistant.academy.rag_eval import (
    QUESTIONS_FILE, RetrievalDataError, RetrievalQuestion, evaluate, load_questions, local_search,
)
from email_assistant.academy.seed_sql import SQL_FILE
from fakes import HashingEmbedder

ROOT = Path(__file__).resolve().parents[2]
FAQ_TEXT = (ROOT / "data/faq/academy_faq.md").read_text(encoding="utf-8")
CHUNKS = load_faq(ROOT / "data/faq/academy_faq.md")


def _leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _leaves(v)
    else:
        yield str(value)


def config_text() -> str:
    """Every fact in config.py, as text (values only, so line numbers and comments cannot back up a number)."""
    facts = [config.DEMO_CLASS_MINUTES, config.DEMO_CLASS_FEE_RUPEES, config.DEMO_SLOT_TIMES,
             config.PLANS, config.BATCHES, config.WORKSHOPS, config.TOURNAMENTS]
    return "\n".join(_leaves(facts))


# ---- the parser ----------------------------------------------------------------------------------

def test_a_question_needs_an_id_line():
    with pytest.raises(FaqError, match="id=.*lang="):
        parse_faq("## T\n### Why?\nBecause.\n")


@pytest.mark.parametrize("markdown,message", [
    ("### Q\n<!-- id=a lang=fr -->\nAnswer.\n", "lang"),
    ("### Q\n<!-- id=a lang=en -->\n", "no answer"),
    ("### Q\n<!-- id=a lang=en -->\nOne.\n### R\n<!-- id=a lang=en -->\nTwo.\n", "used twice"),
    ("# only a title\n", "no questions"),
])
def test_bad_faq_files_are_rejected(markdown, message):
    with pytest.raises(FaqError, match=message):
        parse_faq(markdown)


def test_a_chunk_is_one_question_with_its_answer_and_topic():
    chunks = parse_faq("# Title\nintro text\n## Topic A\n### First?\n<!-- id=first lang=en -->\nLine one.\nLine two.\n## Topic B\n### Second?\n<!-- id=second lang=tenglish -->\nAns.\n")
    assert [(c.id, c.lang, c.topic, c.question, c.answer) for c in chunks] == [
        ("first", "en", "Topic A", "First?", "Line one. Line two."), ("second", "tenglish", "Topic B", "Second?", "Ans.")]
    assert chunks[0].text == "First?\nLine one. Line two." and len(chunks[0].content_hash) == 16
    assert chunks[0].content_hash != chunks[1].content_hash


# ---- the real FAQ file ---------------------------------------------------------------------------

def test_the_faq_has_25_english_and_5_tenglish_questions():
    langs = [c.lang for c in CHUNKS]
    assert langs.count("en") == 25 and langs.count("tenglish") == 5


def test_chunk_ids_are_unique_and_have_no_digits():
    ids = [c.id for c in CHUNKS]
    assert len(set(ids)) == len(ids) and not any(re.search(r"\d", i) for i in ids)


def test_every_number_in_the_faq_appears_in_config_py():
    """The core check: the FAQ states no fee, date, time or count that config.py does not."""
    assert ungrounded(FAQ_TEXT, config_text()) == []


def test_the_number_check_catches_a_changed_fee_and_a_new_time():
    assert set(ungrounded(FAQ_TEXT.replace("₹1234", "₹1299"), config_text())) == {"1299"}  # the fee is in two chunks
    assert "19:00" in ungrounded(FAQ_TEXT.replace("Wednesday at 17:00", "Wednesday at 19:00"), config_text())


def test_every_academy_fact_in_config_is_in_the_faq():
    """The other direction: the FAQ matches the seeded rows (names, fees, timings, dates)."""
    for plan in config.PLANS:
        assert plan["name"] in FAQ_TEXT and f"₹{plan['fee_rupees']}" in FAQ_TEXT
        assert f"{plan['classes_per_month']} classes" in FAQ_TEXT
    for batch in config.BATCHES:
        assert batch["name"] in FAQ_TEXT and batch["days"] in FAQ_TEXT
        start, end = batch["time_range"].split("-")
        assert start in FAQ_TEXT and end in FAQ_TEXT
    for event, fee_key in [(config.WORKSHOPS[0], "fee_rupees"), (config.TOURNAMENTS[0], "entry_fee_rupees")]:
        year, month, day = event["event_date"].split("-")
        assert event["name"] in FAQ_TEXT and f"₹{event[fee_key]}" in FAQ_TEXT
        assert f"{int(day)} {['', 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'][int(month)]} {year}" in FAQ_TEXT
    for times in config.DEMO_SLOT_TIMES.values():
        assert all(t in FAQ_TEXT for t in times)
    assert f"{config.DEMO_CLASS_MINUTES} minutes" in FAQ_TEXT and config.OWNER_NAME in FAQ_TEXT
    sql = (ROOT / SQL_FILE).read_text(encoding="utf-8")
    assert all(p["name"] in sql for p in config.PLANS)   # and those same rows are what the SQL seeds


def test_the_faq_does_not_quote_seat_counts_because_they_go_stale():
    assert not re.search(r"\d+\s+seats?|seats?\s+(left|remaining)?\s*[:=]?\s*\d", FAQ_TEXT, flags=re.I)


def test_the_faq_gives_no_chess_advice_and_says_so():
    chess = next(c for c in CHUNKS if c.id == "chess-advice")
    assert chess.answer.startswith("No.")
    assert not re.search(r"\b(opening|endgame|checkmate|fork|pin|gambit|tactic)s?\b", FAQ_TEXT, flags=re.I)


# ---- the retrieval questions ---------------------------------------------------------------------

QUESTIONS = load_questions(ROOT / QUESTIONS_FILE, CHUNKS)


def test_there_are_15_questions_and_every_expected_chunk_exists():
    assert len(QUESTIONS) == 15
    assert any(q.lang == "tenglish" for q in QUESTIONS)


def test_no_question_is_copied_from_a_faq_heading():
    headings = {c.question.lower().strip("?. ") for c in CHUNKS}
    assert not [q.id for q in QUESTIONS if q.question.lower().strip("?. ") in headings]


@pytest.mark.parametrize("row,message", [
    ('{"id": "x", "lang": "en", "question": "Q?", "expected_chunks": ["no-such-chunk"]}', "not in the FAQ"),
    ('{"id": "x", "lang": "en", "question": "Q?", "expected_chunks": []}', "at least one"),
    ('{"id": "x", "lang": "en", "question": "Q?"}', "bad row"),
    ("not json", "bad row"),
])
def test_bad_question_rows_are_rejected(tmp_path, row, message):
    path = tmp_path / "q.jsonl"
    path.write_text(row + "\n", encoding="utf-8")
    with pytest.raises(RetrievalDataError, match=message):
        load_questions(path, CHUNKS)


def test_duplicate_question_ids_are_rejected(tmp_path):
    row = '{"id": "x", "lang": "en", "question": "Q?", "expected_chunks": ["demo-free"]}'
    path = tmp_path / "q.jsonl"
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(RetrievalDataError, match="duplicate"):
        load_questions(path, CHUNKS)


# ---- recall@k ------------------------------------------------------------------------------------

def test_recall_counts_a_hit_only_inside_the_top_k():
    questions = [RetrievalQuestion("a", "en", "qa", ("x",)), RetrievalQuestion("b", "en", "qb", ("y", "z")),
                 RetrievalQuestion("c", "en", "qc", ("w",)), RetrievalQuestion("d", "en", "qd", ("v",))]
    answers = {"qa": ["x", "p", "q"],          # rank 1
               "qb": ["p", "q", "z"],          # rank 3 (any listed chunk counts)
               "qc": ["p", "q", "r", "w"],     # rank 4: outside the top 3
               "qd": ["p", "q", "r"]}          # never
    result = evaluate(lambda question: answers[question], questions)
    assert (result["recall@1"], result["recall@3"]) == (0.25, 0.5)
    assert result["misses"] == ["c", "d"] and result["n"] == 4


def test_the_in_memory_search_with_the_fake_embedder_finds_obvious_matches():
    search = local_search(CHUNKS, HashingEmbedder())
    assert search("is the demo class free")[0] in {"demo-free", "demo-free-tenglish"}
    assert "complaints" in search("who handles a complaint about safety")


def test_recall_with_the_fake_embedder_is_a_regression_guard_not_a_quality_number():
    """The fake matches shared words only, so its score says nothing about a real model (that is measured on the laptop).
    This floor only catches a broken index, a wrong sort order or an emptied FAQ. It must not be raised by tuning the FAQ to the fake."""
    result = evaluate(local_search(CHUNKS, HashingEmbedder()), QUESTIONS)
    assert result["recall@3"] >= 0.3, result["misses"]
    assert result["recall@1"] <= result["recall@3"]


def test_the_recall_command_prints_and_saves_a_result_without_text(tmp_path, capsys, monkeypatch):
    import dotenv
    import json

    from email_assistant.academy.rag_eval import main

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    search = local_search(CHUNKS, HashingEmbedder())
    assert main(["--out-dir", str(tmp_path)], search=search) == 0
    saved = json.loads(next(tmp_path.glob("rag_recall_*.json")).read_text(encoding="utf-8"))
    assert saved["n"] == 15 and "recall@3" in saved and saved["faq_chunks"] == 30
    assert "recall@3" in capsys.readouterr().out
    assert "Is the demo class free?" not in json.dumps(saved)          # chunk and question ids only


def test_the_recall_command_says_what_is_missing_instead_of_a_traceback(monkeypatch, capsys):
    import dotenv

    from email_assistant.academy import embeddings
    from email_assistant.academy.rag_eval import main

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "_override", None)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert main(["--index", "local"]) == 1
    assert "EMBEDDING_MODEL is not set" in capsys.readouterr().out
