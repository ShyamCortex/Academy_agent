"""FAQ loader: embeds data/faq/academy_faq.md and writes it to the TEST Supabase project (table faq_chunks).

THIS IS THE ONLY PROGRAM THAT WRITES TO SUPABASE (hard rule 4). It runs only on the owner's laptop:
- it lives outside `src/`, so the agent package does not contain it and nothing imports it;
- it refuses to run in CI, in a cloud session or under pytest;
- it needs the TEST project's own service key (ACADEMY_FAQ_LOADER_KEY), which the agent never has;
- it shows the project it is about to write to and asks you to type the project name first.
It never touches the real EduChess project, and no EduChess key is ever used.

    uv run python scripts/ingest_faq.py --dry-run    embed and show what would be written (no key, no network write)
    uv run python scripts/ingest_faq.py              write for real (asks for confirmation; --yes skips the question)

Settings in .env: ACADEMY_SUPABASE_URL (the test project), EMBEDDING_MODEL, ACADEMY_FAQ_LOADER_KEY.
Run it again after you edit the FAQ: rows are upserted by chunk id and rows whose id left the file are deleted.
It prints counts and chunk ids only, never keys.
"""

import argparse
import base64
import json
import os
import sys
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx

from email_assistant.academy.embeddings import Embedder, check_dimension, get_embedder
from email_assistant.academy.faq import FAQ_FILE, FaqChunk, load_faq

URL_VAR = "ACADEMY_SUPABASE_URL"
KEY_VAR = "ACADEMY_FAQ_LOADER_KEY"
TABLE = "faq_chunks"
# If any of these is set, we are not on the owner's laptop.
NOT_LAPTOP_MARKERS = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "CLAUDE_CODE_REMOTE", "CCR_AGENT_PROXY_ENABLED")
RETRY_STATUS = {429, 500, 502, 503, 504}


class LoaderRefused(RuntimeError):
    """The loader will not run. The message says why."""


def refuse_unless_laptop(env: Mapping[str, str]) -> None:
    found = [m for m in NOT_LAPTOP_MARKERS if env.get(m, "").strip()]
    if found:
        raise LoaderRefused(f"This loader runs only on the owner's laptop. It refuses to run here ({', '.join(found)} is set).")


def _jwt_claims(key: str) -> dict:
    parts = key.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return {}


def check_target(url: str, key: str) -> str:
    """Return the project name (the first part of the host) if the URL and key look right, else raise LoaderRefused."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".supabase.co"):
        raise LoaderRefused(f"{URL_VAR} must be the test project's https URL like https://<ref>.supabase.co.")
    project = parts.hostname.split(".")[0]
    claims = _jwt_claims(key)
    if key.startswith("sb_publishable_") or claims.get("role") == "anon":
        raise LoaderRefused(f"{KEY_VAR} is the public (anon) key. The loader needs the TEST project's service key.")
    if not (key.startswith("sb_secret_") or claims.get("role") == "service_role"):
        raise LoaderRefused(f"{KEY_VAR} must be the TEST project's service_role (secret) key.")
    if claims.get("ref") and claims["ref"] != project:
        raise LoaderRefused(f"{KEY_VAR} belongs to a different Supabase project than {URL_VAR}. Refusing to write.")
    return project


def build_rows(chunks: Sequence[FaqChunk], vectors: Sequence[Sequence[float]]) -> list[dict]:
    """One table row per chunk. The vector is sent as text like '[0.1,0.2,...]', which pgvector reads."""
    rows = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        check_dimension(vector)
        rows.append({
            "id": chunk.id, "lang": chunk.lang, "topic": chunk.topic, "question": chunk.question,
            "answer": chunk.answer, "content_hash": chunk.content_hash,
            "embedding": "[" + ",".join(f"{x:.6f}" for x in vector) + "]",
        })
    return rows


def write_rows(http: httpx.Client, rows: Sequence[dict], sleep: Callable[[float], None] = time.sleep) -> None:
    """Upsert the rows, delete rows whose id is no longer in the FAQ, then read the ids back to confirm."""
    def send(method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(4):
            response = http.request(method, path, **kwargs)
            if response.status_code in RETRY_STATUS and attempt < 3:
                sleep(1.0 * 2**attempt)
                continue
            if response.status_code >= 300:
                raise LoaderRefused(f"{method} {path.split('?')[0]} answered HTTP {response.status_code}. Did you run supabase/academy_faq_rag.sql?")
            return response
        raise AssertionError("unreachable")

    send("POST", f"{TABLE}?on_conflict=id", json=list(rows), headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    keep = ",".join(quote(r["id"], safe="") for r in rows)
    send("DELETE", f"{TABLE}?id=not.in.({keep})", headers={"Prefer": "return=minimal"})
    stored = {r["id"]: r["content_hash"] for r in send("GET", f"{TABLE}?select=id,content_hash").json()}
    expected = {r["id"]: r["content_hash"] for r in rows}
    if stored != expected:
        raise LoaderRefused(f"After loading, the table holds {len(stored)} rows but {len(expected)} were expected. Check the table.")


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, embedder: Embedder | None = None,
    transport: httpx.BaseTransport | None = None, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="Load the FAQ into the TEST Supabase project (laptop only).")
    parser.add_argument("--faq", default=str(FAQ_FILE), help="the FAQ markdown file")
    parser.add_argument("--dry-run", action="store_true", help="embed and report, write nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    if env is None:
        from dotenv import load_dotenv

        load_dotenv(".env")
        env = os.environ
    try:
        refuse_unless_laptop(env)
        chunks = load_faq(args.faq)
        say(f"Read {len(chunks)} FAQ chunks from {args.faq}.")
        embedder = embedder or get_embedder()
        vectors = embedder.embed_documents([c.text for c in chunks])
        rows = build_rows(chunks, vectors)
        say(f"Embedded them ({len(rows[0]['embedding'].split(','))} numbers each).")
        if args.dry_run:
            say("Dry run: nothing was written. Chunk ids: " + ", ".join(c.id for c in chunks))
            return 0

        url, key = env.get(URL_VAR, "").strip(), env.get(KEY_VAR, "").strip()
        if not url or not key:
            raise LoaderRefused(f"Set {URL_VAR} (the test project's URL) and {KEY_VAR} (that project's service key) in .env.")
        project = check_target(url, key)
        say(f"About to write {len(rows)} rows to table '{TABLE}' of Supabase project '{project}'.")
        if not args.yes and ask(f"This must be the TEST project, never EduChess. Type the project name ({project}) to continue: ").strip() != project:
            say("Not confirmed. Nothing was written.")
            return 1
        with httpx.Client(
            base_url=url.rstrip("/") + "/rest/v1/", timeout=30.0, transport=transport,
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        ) as http:
            write_rows(http, rows)
        say(f"Done: {len(rows)} FAQ chunks are in '{TABLE}'.")
        return 0
    except LoaderRefused as exc:
        say(f"Refused: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - say what kind of failure, never print a key or a response body
        say(f"Failed: {type(exc).__name__}: {exc}" if type(exc).__module__.startswith("email_assistant") else f"Failed: {type(exc).__name__}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
