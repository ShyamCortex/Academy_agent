"""recall@k for the FAQ search: for each question, is a correct chunk among the k closest results?

    recall@3 = (questions with a correct chunk in the top 3) / (all questions)

The 15 questions in `data/faq/retrieval_questions.jsonl` are written in parents' words (not copied from the
FAQ headings), and each lists the chunk id(s) that answer it. More than one id is listed when two chunks
both answer the question. Two ways to run it on your laptop:

    uv run python -m email_assistant.academy.rag_eval --index local    embed the FAQ file in memory (no database).
                                                                       Tests the embedding model alone, so run it BEFORE loading.
    uv run python -m email_assistant.academy.rag_eval                  go through the real search: the test project's
                                                                       match_faq function with the anon key. The full path.

A dated result file (question ids and chunk ids only) is saved in docs/agent/results/.
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from email_assistant.academy import config
from email_assistant.academy.faq import FAQ_FILE, FaqChunk, load_faq

QUESTIONS_FILE = Path("data/faq/retrieval_questions.jsonl")
KS = (1, 3)


class RetrievalDataError(ValueError):
    """The question file is not valid, or a question names a chunk that is not in the FAQ."""


@dataclass(frozen=True)
class RetrievalQuestion:
    id: str
    lang: str
    question: str
    expected_chunks: tuple[str, ...]


def load_questions(path: str | Path = QUESTIONS_FILE, chunks: Sequence[FaqChunk] | None = None) -> list[RetrievalQuestion]:
    """Read and check the question file. Every expected chunk id must exist in the FAQ."""
    known = {c.id for c in (chunks if chunks is not None else load_faq())}
    questions, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{Path(path).name} line {number}"
        try:
            row = json.loads(line)
            question = RetrievalQuestion(row["id"], row["lang"], row["question"], tuple(row["expected_chunks"]))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RetrievalDataError(f"{where}: bad row ({type(exc).__name__}).") from None
        if question.id in seen:
            raise RetrievalDataError(f"{where}: duplicate id {question.id!r}.")
        if not question.expected_chunks or not question.question.strip():
            raise RetrievalDataError(f"{where}: needs a question and at least one expected chunk.")
        unknown = [c for c in question.expected_chunks if c not in known]
        if unknown:
            raise RetrievalDataError(f"{where}: chunk id(s) not in the FAQ: {unknown}.")
        seen.add(question.id)
        questions.append(question)
    if not questions:
        raise RetrievalDataError(f"{Path(path).name}: no questions.")
    return questions


def evaluate(search: Callable[[str], list[str]], questions: Sequence[RetrievalQuestion], ks: Sequence[int] = KS) -> dict:
    """Run every question through `search` (it returns chunk ids, best first) and score recall@k."""
    per_question = []
    for q in questions:
        returned = search(q.question)
        ranks = [returned.index(c) + 1 for c in q.expected_chunks if c in returned]
        per_question.append({"id": q.id, "lang": q.lang, "expected": list(q.expected_chunks), "returned": returned,
                             "first_hit_rank": min(ranks) if ranks else None})
    n = len(per_question)
    recall = {f"recall@{k}": sum(1 for p in per_question if p["first_hit_rank"] and p["first_hit_rank"] <= k) / n for k in ks}
    return {"n": n, **recall, "misses": [p["id"] for p in per_question if not p["first_hit_rank"] or p["first_hit_rank"] > max(ks)],
            "per_question": per_question}


def local_search(chunks: Sequence[FaqChunk], embedder, count: int = max(KS)) -> Callable[[str], list[str]]:
    """A search that embeds the FAQ in memory, with no database. Tests the embedding model by itself."""
    vectors = embedder.embed_documents([c.text for c in chunks])

    def cosine(a: Sequence[float], b: Sequence[float]) -> float:
        return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)

    def search(question: str) -> list[str]:
        query = embedder.embed_query(question)
        ranked = sorted(zip(chunks, vectors), key=lambda pair: -cosine(query, pair[1]))
        return [c.id for c, _ in ranked[:count]]

    return search


def remote_search(count: int = max(KS)) -> Callable[[str], list[str]]:
    """The real path: embed the question, then call match_faq in the test project with the anon key."""
    from email_assistant.academy.knowledge_tools import search_faq

    return lambda question: [r["id"] for r in search_faq(question, count)]


def format_report(result: dict, index: str) -> str:
    lines = [f"FAQ search recall ({index} index), {result['n']} questions:"]
    lines += [f"  {key}: {value:.0%}" for key, value in result.items() if key.startswith("recall@")]
    if result["misses"]:
        lines.append("  Not in the top results: " + ", ".join(result["misses"]))
        for p in result["per_question"]:
            if p["id"] in result["misses"]:
                lines.append(f"    {p['id']}: wanted one of {p['expected']}, got {p['returned']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, search: Callable[[str], list[str]] | None = None) -> int:
    parser = argparse.ArgumentParser(description="recall@k of the FAQ search on the 15 labelled questions.")
    parser.add_argument("--index", choices=["remote", "local"], default="remote")
    parser.add_argument("--questions", default=str(QUESTIONS_FILE))
    parser.add_argument("--faq", default=str(FAQ_FILE))
    parser.add_argument("--out-dir", default="docs/agent/results")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(".env")
    from email_assistant.academy.data_client import DataSourceError
    from email_assistant.academy.embeddings import EmbeddingError
    from email_assistant.academy.settings import AcademyConfigError

    chunks = load_faq(args.faq)
    questions = load_questions(args.questions, chunks)
    try:
        if search is None:
            if args.index == "local":
                from email_assistant.academy.embeddings import get_embedder

                search = local_search(chunks, get_embedder())
            else:
                search = remote_search()
        result = evaluate(search, questions)
    except (AcademyConfigError, EmbeddingError, DataSourceError) as exc:
        print(f"Not ready: {exc}")
        return 1
    print(format_report(result, args.index))
    now = datetime.now(config.TIMEZONE)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rag_recall_{now.strftime('%Y%m%d_%H%M%S')}.json"
    record = {"date": now.isoformat(timespec="seconds"), "index": args.index, "embedding_model": _model_name(), "faq_chunks": len(chunks), **result}
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved: {path}")
    return 0


def _model_name() -> str:
    import os

    from email_assistant.academy import settings

    return os.environ.get(settings.EMBEDDING_MODEL_VAR, "")


if __name__ == "__main__":
    sys.exit(main())
