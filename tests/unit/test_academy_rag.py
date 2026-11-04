"""Light RAG: the FAQ search tool, its read-only path to the database, and the pgvector SQL."""

import json
import re
import sys
import types
from pathlib import Path

import httpx
import pytest

from academy_helpers import ANON_KEY, TEST_URL, Run, triage_says
from email_assistant.academy import data_client, embeddings, knowledge_tools, settings
from email_assistant.academy.data_client import ReadOnlyRest, ReadOnlyViolation, use_data_client
from email_assistant.academy.embeddings import EMBEDDING_DIM, EmbeddingError, FastEmbedEmbedder, check_dimension, use_embedder
from email_assistant.academy.guards import READ_ONLY_TOOLS, needs_approval
from email_assistant.academy.tools import ACADEMY_TOOLS, TOOLS_PROMPT
from email_assistant.models import use_fake_models
from fakes import HashingEmbedder, ScriptedModel, tool_call_message

ROOT = Path(__file__).resolve().parents[2]


def search(question="Is the demo class free?"):
    return json.loads(knowledge_tools.search_knowledge.invoke({"question": question}))


# ---- the tool ------------------------------------------------------------------------------------

def test_the_search_returns_the_three_closest_chunks_with_a_source(academy_world):
    result = search("is the demo class free")
    assert result["source"] == "supabase test project: faq_chunks"
    assert len(result["results"]) == 3
    assert result["results"][0]["id"] in {"demo-free", "demo-free-tenglish"}
    sims = [r["similarity"] for r in result["results"]]
    assert sims == sorted(sims, reverse=True)
    assert set(result["results"][0]) == {"id", "question", "answer", "similarity"}


def test_the_search_is_one_plain_get_of_match_faq_with_the_anon_key(academy_world):
    search("how much is the monthly fee")
    (request,) = academy_world.server.requests
    assert request.method == "GET" and request.content == b""
    assert request.url.path == "/rest/v1/rpc/match_faq"
    assert request.headers["apikey"] == ANON_KEY
    assert request.url.params["match_count"] == "3"
    vector = json.loads(request.url.params["query_embedding"])
    assert len(vector) == EMBEDDING_DIM and all(isinstance(x, float) for x in vector)


def test_a_long_question_is_cut_before_it_is_embedded(academy_world):
    fake = HashingEmbedder()
    with use_embedder(fake):
        search("fee " * 1000)
    assert len(fake.queries[0]) == knowledge_tools.MAX_QUESTION_CHARS


def test_a_database_error_comes_back_as_an_error_result_without_details():
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "secret"})), sleep=lambda s: None)
    with use_data_client(client):
        result = search()
    assert result["error"] == "The data source answered with HTTP 404." and "results" not in result and "secret" not in json.dumps(result)


def test_a_model_with_the_wrong_vector_size_is_reported_not_sent(academy_world):
    class Small(HashingEmbedder):
        def __init__(self):
            super().__init__(dim=8)

    with use_embedder(Small()):
        result = search()
    assert "384" in result["error"] and "8" in result["error"]
    assert academy_world.server.requests == []  # nothing was sent to the database


def test_a_missing_embedding_model_setting_names_the_variable(monkeypatch):
    monkeypatch.setattr(embeddings, "_override", None)
    monkeypatch.setattr(embeddings, "_cached", None)
    monkeypatch.delenv(settings.EMBEDDING_MODEL_VAR, raising=False)
    with pytest.raises(settings.AcademyConfigError, match=settings.EMBEDDING_MODEL_VAR):
        embeddings.get_embedder()


def test_get_embedder_builds_the_model_named_in_the_environment_once(monkeypatch):
    monkeypatch.setattr(embeddings, "_override", None)
    monkeypatch.setattr(embeddings, "_cached", None)
    monkeypatch.setenv(settings.EMBEDDING_MODEL_VAR, "some/model")
    first = embeddings.get_embedder()
    assert isinstance(first, FastEmbedEmbedder) and first.model_name == "some/model" and embeddings.get_embedder() is first
    monkeypatch.setenv(settings.EMBEDDING_MODEL_VAR, "other/model")
    assert embeddings.get_embedder().model_name == "other/model"


# ---- the fastembed wrapper (with a stand-in for the package) --------------------------------------

def _fake_fastembed(monkeypatch, *, supported=True):
    seen = {}

    class TextEmbedding:
        def __init__(self, model_name, cache_dir=None):
            if not supported:
                raise ValueError("Model not supported")
            seen.update(model_name=model_name, cache_dir=cache_dir)

        def query_embed(self, texts):
            seen["query"] = list(texts)
            return iter([[0.5] * EMBEDDING_DIM])

        def passage_embed(self, texts):
            seen["passages"] = list(texts)
            return iter([[0.25] * EMBEDDING_DIM for _ in texts])

    module = types.ModuleType("fastembed")
    module.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return seen


def test_fastembed_uses_query_embedding_for_questions_and_passage_embedding_for_chunks(monkeypatch):
    seen = _fake_fastembed(monkeypatch)
    monkeypatch.setenv(settings.EMBEDDING_CACHE_VAR, "/some/cache")
    embedder = FastEmbedEmbedder("some/model")
    assert embedder.embed_query("fees?") == [0.5] * EMBEDDING_DIM
    assert embedder.embed_documents(["a", "b"]) == [[0.25] * EMBEDDING_DIM] * 2
    assert seen == {"model_name": "some/model", "cache_dir": "/some/cache", "query": ["fees?"], "passages": ["a", "b"]}


def test_an_unsupported_model_name_gives_a_clear_error(monkeypatch):
    _fake_fastembed(monkeypatch, supported=False)
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL.*not a model fastembed supports"):
        FastEmbedEmbedder("nope").embed_query("x")


def test_check_dimension():
    check_dimension([0.0] * EMBEDDING_DIM)
    with pytest.raises(EmbeddingError):
        check_dimension([0.0] * 5)


# ---- the read-only client ------------------------------------------------------------------------

def test_match_faq_can_ask_for_one_to_five_results_only():
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    for bad in (0, 6, -1):
        with pytest.raises(ValueError):
            client.match_faq([0.1] * EMBEDDING_DIM, bad)
    assert client.match_faq([0.1] * EMBEDDING_DIM, 5) == []


def test_the_client_still_cannot_write_even_by_reaching_past_match_faq():
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(ReadOnlyViolation):
        client._http.post("rpc/match_faq", json={})
    assert data_client.FAQ_MATCH_FUNCTION == "match_faq" and not hasattr(client, "rpc")


# ---- the tool in the agent -----------------------------------------------------------------------

def test_search_knowledge_is_read_only_and_runs_without_the_owner():
    assert "search_knowledge" in READ_ONLY_TOOLS and not needs_approval("search_knowledge")
    agent = ScriptedModel(tool_call_message("search_knowledge", question="do you give discounts"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("fees_plans"), agent=agent):
        result = Run().start()
    assert "__interrupt__" not in result
    tool_message = next(m for m in result["messages"] if m.type == "tool")
    assert json.loads(tool_message.content)["results"]


def test_search_knowledge_is_one_of_the_agents_tools_and_is_described_in_the_prompt():
    assert "search_knowledge" in {t.name for t in ACADEMY_TOOLS}
    for t in ACADEMY_TOOLS:
        assert t.name in TOOLS_PROMPT, f"{t.name} is missing from TOOLS_PROMPT"
    assert "Do not make up an answer" in TOOLS_PROMPT


def test_the_mcp_server_offers_search_knowledge():
    from email_assistant.academy.mcp_bridge import in_process_client

    client = in_process_client()
    try:
        assert "search_knowledge" in client.list_tool_names()
        assert json.loads(client.call("search_knowledge", {"question": "is the demo free"}))["results"]
    finally:
        client.close()


def test_the_mcp_server_is_started_with_the_embedding_settings_and_no_secrets():
    from email_assistant.academy.mcp_bridge import SERVER_ENV_VARS

    assert settings.EMBEDDING_MODEL_VAR in SERVER_ENV_VARS
    assert not [v for v in SERVER_ENV_VARS if re.search(r"LOADER|SERVICE|SECRET|GROQ|GOOGLE_API|LANGSMITH", v)]


# ---- the SQL file ---------------------------------------------------------------------------------

SQL = (ROOT / "supabase/academy_faq_rag.sql").read_text(encoding="utf-8")
SQL_CODE = "\n".join(line.split("--")[0] for line in SQL.splitlines())


def test_faq_sql_has_row_level_security_and_gives_anon_select_only():
    assert "alter table faq_chunks enable row level security" in SQL_CODE
    assert re.findall(r'create policy "[^"]+" on (\w+) for (\w+) to (\w+)', SQL_CODE) == [("faq_chunks", "select", "anon")]
    assert not re.search(r"for\s+(insert|update|delete|all)\b", SQL_CODE, flags=re.I)
    assert re.findall(r"grant\s+(\w+)\s+on\s+(?!function)(\w+)", SQL_CODE, flags=re.I) == [("select", "faq_chunks")]
    assert "revoke all on faq_chunks from anon, authenticated" in SQL_CODE
    assert not re.search(r"\b(drop|truncate)\b", SQL_CODE, flags=re.I)


def test_faq_sql_search_function_is_stable_so_it_can_be_called_with_get():
    function = re.search(r"create function match_faq\(.*?\$\$;", SQL_CODE, flags=re.S).group(0)
    assert re.search(r"^\s*stable\s*$", function, flags=re.M)
    assert "security definer" not in function.lower()           # it runs with the caller's (anon's) rights
    assert "limit least(greatest(match_count, 1), 5)" in function
    assert f"create function {data_client.FAQ_MATCH_FUNCTION}(" in SQL_CODE
    assert re.search(r"grant execute on function match_faq\(.*\) to anon;", SQL_CODE)


def test_faq_sql_vector_size_matches_the_embedder_and_the_columns_match_the_loader():
    assert set(re.findall(r"vector\((\d+)\)", SQL_CODE)) == {str(EMBEDDING_DIM)}
    table = re.search(r"create table faq_chunks \((.*?)\n\);", SQL_CODE, flags=re.S).group(1)
    columns = {line.split()[0] for line in table.splitlines() if line.strip()}
    assert {"id", "lang", "topic", "question", "answer", "content_hash", "embedding"} <= columns
    returned = re.search(r"returns table \((.*?)\)", SQL_CODE, flags=re.S).group(1)
    assert [part.split()[0] for part in returned.split(",")] == ["id", "lang", "topic", "question", "answer", "similarity"]
