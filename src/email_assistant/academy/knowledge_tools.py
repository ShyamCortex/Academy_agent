"""FAQ search (light RAG): find the FAQ answers closest to a question. Read-only.

    question --embed (local model)--> vector --GET match_faq (anon key)--> top 3 FAQ chunks

RAG (retrieval-augmented generation) means the model reads the retrieved FAQ text instead of answering from
memory. Every result carries a `source` field and goes through the same number check as every other tool:
a number in a reply must appear in a tool result of the same run, and these results are tool results.
"""

import json

from langchain_core.tools import tool

from email_assistant.academy.data_client import DataSourceError, get_data_client
from email_assistant.academy.embeddings import EmbeddingError, check_dimension, get_embedder

SOURCE = "supabase test project: faq_chunks"
TOP_K = 3
MAX_QUESTION_CHARS = 500


def search_faq(question: str, count: int = TOP_K) -> list[dict]:
    """The `count` closest FAQ chunks, best first. Raises EmbeddingError or DataSourceError."""
    vector = get_embedder().embed_query(question.strip()[:MAX_QUESTION_CHARS])
    check_dimension(vector)
    rows = get_data_client().match_faq(vector, count)
    return [
        {"id": r["id"], "question": r["question"], "answer": r["answer"], "similarity": round(float(r["similarity"]), 3)}
        for r in rows
    ]


@tool
def search_knowledge(question: str) -> str:
    """Search the academy FAQ for general questions (how to book, payment, discounts, complaints, what the assistant can do). Returns the 3 closest FAQ answers."""
    try:
        results = search_faq(question)
    except (DataSourceError, EmbeddingError) as exc:
        return json.dumps({"source": SOURCE, "error": str(exc)})
    return json.dumps({"source": SOURCE, "results": results}, ensure_ascii=False)


KNOWLEDGE_TOOLS = [search_knowledge]
