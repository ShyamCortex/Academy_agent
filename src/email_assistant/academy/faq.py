"""Read the FAQ file into chunks. One question plus its answer is one chunk.

File format (`data/faq/academy_faq.md`):

    ## Topic name                        a group of questions (becomes the chunk's topic)
    ### The question, as a parent asks it
    <!-- id=some-stable-id lang=en -->   the chunk id (letters, digits, dashes) and the language
    The answer, one or more lines.

Why one Q&A per chunk: each answer is one to three sentences and is only useful next to its question.
Cutting it into smaller pieces would separate an answer from the words a parent is likely to use.
Chunk ids are stable: the loader upserts by id, so editing an answer replaces its row.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

FAQ_FILE = Path("data/faq/academy_faq.md")
LANGUAGES = ("en", "tenglish")
_META = re.compile(r"^<!--\s*id=([a-z0-9][a-z0-9-]*)\s+lang=(\w+)\s*-->$")


class FaqError(ValueError):
    """The FAQ file does not follow the format. The message says which question."""


@dataclass(frozen=True)
class FaqChunk:
    id: str
    lang: str
    topic: str
    question: str
    answer: str

    @property
    def text(self) -> str:
        """What gets embedded: the question and the answer together."""
        return f"{self.question}\n{self.answer}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


def parse_faq(markdown: str) -> list[FaqChunk]:
    """Split the FAQ text into chunks. Raises FaqError if a question has no id line, no answer, or a repeated id."""
    chunks: list[FaqChunk] = []
    topic = ""
    pending: dict | None = None  # the question being read: {"question", "meta", "lines"}

    def finish() -> None:
        nonlocal pending
        if pending is None:
            return
        where = f"question {pending['question']!r}"
        if pending["meta"] is None:
            raise FaqError(f"{where}: the line after it must be <!-- id=... lang=... -->.")
        chunk_id, lang = pending["meta"]
        answer = " ".join(" ".join(pending["lines"]).split())
        if lang not in LANGUAGES:
            raise FaqError(f"{where}: lang {lang!r} must be one of {LANGUAGES}.")
        if not answer:
            raise FaqError(f"{where}: no answer text.")
        if any(c.id == chunk_id for c in chunks):
            raise FaqError(f"{where}: id {chunk_id!r} is used twice.")
        chunks.append(FaqChunk(chunk_id, lang, topic, pending["question"], answer))
        pending = None

    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            finish()
            pending = {"question": line[4:].strip(), "meta": None, "lines": []}
        elif line.startswith("## ") or line.startswith("# "):
            finish()
            if line.startswith("## "):
                topic = line[3:].strip()
        elif pending is not None and line:
            meta = _META.match(line) if pending["meta"] is None and not pending["lines"] else None
            if meta:
                pending["meta"] = (meta.group(1), meta.group(2))
            else:
                pending["lines"].append(line)
    finish()
    if not chunks:
        raise FaqError("The FAQ has no questions.")
    return chunks


def load_faq(path: str | Path = FAQ_FILE) -> list[FaqChunk]:
    return parse_faq(Path(path).read_text(encoding="utf-8"))
