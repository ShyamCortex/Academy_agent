"""The FAQ loader is the only writer (hard rule 4). It runs on the owner's laptop only.

These tests run the loader's code against a pretend server inside this process: no network, no real key,
nothing is written anywhere. They prove it refuses to run in the wrong place and writes only the FAQ table.
"""

import importlib.util
import json
import re
from pathlib import Path

import httpx
import pytest

from email_assistant.academy.embeddings import EMBEDDING_DIM
from email_assistant.academy.faq import load_faq
from fakes import HashingEmbedder
from academy_helpers import make_jwt

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ingest_faq", ROOT / "scripts" / "ingest_faq.py")
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)

URL = "https://abcdefgh.supabase.co"
KEY = "sb_secret_notarealkey123"
ENV = {loader.URL_VAR: URL, loader.KEY_VAR: KEY}
CHUNKS = load_faq()


class PretendTable:
    """A pretend faq_chunks table behind PostgREST. Records every request."""

    def __init__(self, stored=None):
        self.rows = dict(stored or {})
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/rest/v1/faq_chunks", request.url.path
        if request.method == "POST":
            for row in json.loads(request.content):
                self.rows[row["id"]] = row
            return httpx.Response(201)
        if request.method == "DELETE":
            keep = request.url.params["id"][len("not.in.("):-1].split(",")
            self.rows = {k: v for k, v in self.rows.items() if k in keep}
            return httpx.Response(204)
        return httpx.Response(200, json=[{"id": k, "content_hash": v["content_hash"]} for k, v in self.rows.items()])


def run(argv=(), env=ENV, answers=(), table=None):
    table = table or PretendTable()
    said, asked = [], list(answers)
    code = loader.main(list(argv), env=env, embedder=HashingEmbedder(), transport=httpx.MockTransport(table.handle),
                       ask=lambda prompt: asked.pop(0), say=said.append)
    return code, table, "\n".join(said)


# ---- where it may run ----------------------------------------------------------------------------

@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "CLAUDE_CODE_REMOTE", "CCR_AGENT_PROXY_ENABLED"])
def test_it_refuses_to_run_in_ci_in_a_cloud_session_or_under_pytest(marker):
    code, table, output = run(["--yes"], env={**ENV, marker: "1"})
    assert code == 1 and "only on the owner's laptop" in output and table.requests == []


# ---- the target and the key ----------------------------------------------------------------------

@pytest.mark.parametrize("env,message", [
    ({}, "Set ACADEMY_SUPABASE_URL"),
    ({loader.URL_VAR: URL}, "ACADEMY_FAQ_LOADER_KEY"),
    ({loader.URL_VAR: "http://abcdefgh.supabase.co", loader.KEY_VAR: KEY}, "https URL"),
    ({loader.URL_VAR: "https://example.com", loader.KEY_VAR: KEY}, "https URL"),
    ({loader.URL_VAR: URL, loader.KEY_VAR: "sb_publishable_abc"}, "public \\(anon\\) key"),
    ({loader.URL_VAR: URL, loader.KEY_VAR: make_jwt("anon")}, "public \\(anon\\) key"),
    ({loader.URL_VAR: URL, loader.KEY_VAR: "random-string"}, "service_role"),
])
def test_it_refuses_a_missing_setting_a_bad_url_or_a_key_that_is_not_a_service_key(env, message):
    code, table, output = run(["--yes"], env=env)
    assert code == 1 and re.search(message, output) and table.requests == []


def test_it_refuses_a_key_from_a_different_project():
    import base64

    def jwt(claims):
        part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
        return f"{part({'alg': 'HS256'})}.{part(claims)}.sig"

    other = {loader.URL_VAR: URL, loader.KEY_VAR: jwt({"role": "service_role", "ref": "someotherproject"})}
    code, table, output = run(["--yes"], env=other)
    assert code == 1 and "different Supabase project" in output and table.requests == []
    same = {loader.URL_VAR: URL, loader.KEY_VAR: jwt({"role": "service_role", "ref": "abcdefgh"})}
    assert run(["--yes"], env=same)[0] == 0


def test_it_does_not_write_unless_you_type_the_project_name():
    code, table, output = run([], answers=["yes"])
    assert code == 1 and "Not confirmed" in output and table.requests == []
    code, table, output = run([], answers=["abcdefgh"])
    assert code == 0 and table.requests


# ---- what it writes ------------------------------------------------------------------------------

def test_a_dry_run_embeds_and_reports_but_needs_no_key_and_sends_nothing():
    code, table, output = run(["--dry-run"], env={})
    assert code == 0 and table.requests == [] and "Dry run" in output and "demo-free" in output


def test_it_upserts_every_chunk_and_touches_only_the_faq_table():
    code, table, output = run(["--yes"])
    assert code == 0
    assert [r.method for r in table.requests] == ["POST", "DELETE", "GET"]
    assert {r.url.path for r in table.requests} == {"/rest/v1/faq_chunks"}
    assert all(r.headers["apikey"] == KEY for r in table.requests)
    assert set(table.rows) == {c.id for c in CHUNKS}
    row = table.rows["fees-plans"]
    assert set(row) == {"id", "lang", "topic", "question", "answer", "content_hash", "embedding"}
    assert len(row["embedding"].strip("[]").split(",")) == EMBEDDING_DIM
    assert "30 FAQ chunks" in output


def test_a_chunk_removed_from_the_faq_is_deleted_from_the_table_and_a_changed_one_is_replaced():
    table = PretendTable({"old-removed": {"content_hash": "x"}, "demo-free": {"id": "demo-free", "content_hash": "stale"}})
    code, table, _ = run(["--yes"], table=table)
    assert code == 0 and "old-removed" not in table.rows
    assert table.rows["demo-free"]["content_hash"] == next(c.content_hash for c in CHUNKS if c.id == "demo-free")


def test_it_fails_loudly_when_the_table_does_not_hold_what_was_sent():
    class Lossy(PretendTable):
        def handle(self, request):
            response = super().handle(request)
            if request.method == "POST":
                self.rows.pop("demo-free")
            return response

    code, _, output = run(["--yes"], table=Lossy())
    assert code == 1 and "expected" in output


def test_a_missing_table_points_at_the_sql_file():
    said = []
    code = loader.main(["--yes"], env=ENV, embedder=HashingEmbedder(), transport=httpx.MockTransport(lambda r: httpx.Response(404)), say=said.append)
    assert code == 1 and "academy_faq_rag.sql" in " ".join(said)


def test_a_rate_limited_server_is_retried(monkeypatch):
    monkeypatch.setattr(loader.time, "sleep", lambda s: None)
    table, seen = PretendTable(), {"first": True}

    def handler(request):
        if request.method == "POST" and seen.pop("first", False):
            return httpx.Response(429)
        return table.handle(request)

    code = loader.main(["--yes"], env=ENV, embedder=HashingEmbedder(), transport=httpx.MockTransport(handler), say=lambda s: None)
    assert code == 0 and set(table.rows) == {c.id for c in CHUNKS}


def test_the_key_is_never_printed():
    for argv, env in [(["--yes"], ENV), (["--dry-run"], ENV), (["--yes"], {**ENV, "CI": "1"})]:
        assert KEY not in run(argv, env=env)[2]


# ---- the boundary: the agent package cannot reach the loader ------------------------------------

def test_the_loader_is_outside_the_agent_package_and_nothing_in_src_mentions_it():
    src = ROOT / "src"
    assert not (src / "email_assistant" / "academy" / "ingest_faq.py").exists()
    offenders = [p for p in src.rglob("*.py") if re.search(r"ingest_faq|ACADEMY_FAQ_LOADER_KEY", p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert 'packages = ["email_assistant"]' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")  # scripts/ is not shipped


def test_no_agent_or_test_code_other_than_the_loader_tests_names_the_loader_key():
    allowed = {ROOT / "scripts" / "ingest_faq.py", Path(__file__).resolve(), ROOT / "docs" / "agent" / "LAPTOP_SETUP.md"}
    files = [p for folder in ("src", "tests") for p in (ROOT / folder).rglob("*.py")]
    assert [p for p in files if p not in allowed and "FAQ_LOADER_KEY" in p.read_text(encoding="utf-8")] == []
