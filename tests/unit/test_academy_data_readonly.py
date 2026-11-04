"""Hard rule 4: the agent reads the TEST Supabase project with the anon key, and can never write.

Three kinds of check:
- behaviour: every data tool is run against a pretend server and every request it sends is inspected;
- structure: the client refuses non-GET requests and any key that is not provably an anon key;
- source scan: the data code never mentions a service key or a write method (this fails if someone adds one).
"""

import ast
import json
import re
from pathlib import Path

import httpx
import pytest

from academy_helpers import ANON_KEY, TEST_URL, SeedServer, make_jwt
from email_assistant.academy import config, data_client, data_tools, settings
from email_assistant.academy.data_client import (
    DataSourceError, ReadOnlyRest, ReadOnlyViolation, check_anon_key, use_data_client,
)
from email_assistant.academy.seed_sql import SQL_FILE, render_seed, seed_block

ACADEMY = Path(data_client.__file__).parent
ROOT = ACADEMY.parents[2]
SQL = (ROOT / SQL_FILE).read_text(encoding="utf-8")
DATA_TOOL_NAMES = ["get_plans", "get_batch_timings", "get_workshops", "get_tournaments"]


def call(name):
    return json.loads(next(t for t in data_tools.DATA_TOOLS if t.name == name).invoke({}))


# ---- behaviour -----------------------------------------------------------------------------------

def test_each_data_tool_returns_the_seeded_rows_with_a_source(academy_world):
    plans = call("get_plans")
    assert plans["source"] == "supabase test project: plans"
    assert plans["plans"] == [{k: p[k] for k in ("name", "classes_per_month", "fee_rupees")} for p in config.PLANS]
    assert [b["seats_left"] for b in call("get_batch_timings")["batches"]] == [b["seats_left"] for b in config.BATCHES]
    assert call("get_workshops")["workshops"][0]["fee_rupees"] == config.WORKSHOPS[0]["fee_rupees"]
    assert call("get_tournaments")["tournaments"][0]["entry_fee_rupees"] == config.TOURNAMENTS[0]["entry_fee_rupees"]


def test_every_request_every_tool_sends_is_a_plain_get_with_the_anon_key(academy_world):
    for name in DATA_TOOL_NAMES:
        call(name)
    requests = academy_world.server.requests
    assert len(requests) == 4
    for request in requests:
        assert request.method == "GET"
        assert request.content == b""  # no body
        assert request.url.host == "test-project.supabase.co" and request.url.path.startswith("/rest/v1/")
        assert request.headers["apikey"] == ANON_KEY and request.headers["authorization"] == f"Bearer {ANON_KEY}"
    assert {r.url.path.rsplit("/", 1)[-1] for r in requests} == {"plans", "batches", "workshops", "tournaments"}


def test_workshops_and_tournaments_ask_only_for_events_from_today_in_india(academy_world):
    call("get_workshops")
    assert academy_world.server.requests[0].url.params["event_date"] == "gte.2026-11-04"


def test_past_events_are_not_returned(academy_world, monkeypatch):
    from email_assistant.academy import dates
    monkeypatch.setattr(dates, "now_ist", lambda: dates.parse_local_datetime("2026-12-01T10:00"))
    assert call("get_workshops")["workshops"] == []  # the seeded workshop is on 22 Nov


def test_a_data_error_comes_back_as_an_error_result_without_details():
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "secret"})))
    with use_data_client(client):
        result = call("get_plans")
    assert result["error"] == "The data source answered with HTTP 401." and "plans" not in result and "secret" not in json.dumps(result)


def test_a_flaky_server_is_tried_once_more_then_reported():
    attempts, pauses = [], []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) == 1 else httpx.Response(200, json=[])

    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(handler), sleep=pauses.append)
    assert client.select("plans", "name") == [] and len(attempts) == 2 and pauses == [0.5]
    dead = ReadOnlyRest(TEST_URL, ANON_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=lambda s: None)
    with pytest.raises(DataSourceError, match="503"):
        dead.select("plans", "name")


def test_a_missing_url_or_key_names_the_variable(monkeypatch):
    monkeypatch.delenv(settings.SUPABASE_ANON_KEY_VAR, raising=False)
    monkeypatch.setenv(settings.SUPABASE_URL_VAR, TEST_URL)
    with pytest.raises(settings.AcademyConfigError, match=settings.SUPABASE_ANON_KEY_VAR):
        ReadOnlyRest.from_env()
    monkeypatch.delenv(settings.SUPABASE_URL_VAR)
    with pytest.raises(settings.AcademyConfigError, match=settings.SUPABASE_URL_VAR):
        ReadOnlyRest.from_env()


# ---- structure -----------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_transport_blocks_every_write_even_through_the_inner_client(method):
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=SeedServer().transport)
    with pytest.raises(ReadOnlyViolation):
        getattr(client._http, method)("plans")  # someone reaching past `select`: still blocked


def test_the_client_has_no_public_method_except_select():
    public = [n for n in dir(ReadOnlyRest) if not n.startswith("_")]
    assert sorted(public) == ["from_env", "select"]


def test_only_known_tables_can_be_read():
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=SeedServer().transport)
    with pytest.raises(ValueError, match="not readable"):
        client.select("students", "name")


@pytest.mark.parametrize("key", [make_jwt("service_role"), make_jwt("authenticated"), make_jwt(""), "sb_secret_abc123",
                                 "just-a-string", "", "a.b.c", "sb_publishable"])
def test_a_key_that_is_not_an_anon_key_is_refused(key):
    with pytest.raises(settings.AcademyConfigError, match="anon"):
        check_anon_key(key)
    with pytest.raises(settings.AcademyConfigError):
        ReadOnlyRest(TEST_URL, key)


def test_the_two_kinds_of_public_key_are_accepted():
    check_anon_key(make_jwt("anon"))
    check_anon_key("sb_publishable_abc123")
    seen = []
    ReadOnlyRest(TEST_URL, "sb_publishable_abc123", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=[]))).select("plans", "name")
    assert seen[0].headers["apikey"] == "sb_publishable_abc123" and "authorization" not in seen[0].headers


@pytest.mark.parametrize("url", ["http://evil.example.com", "ftp://x.supabase.co", "not a url", ""])
def test_the_url_must_be_https(url):
    with pytest.raises(settings.AcademyConfigError, match=settings.SUPABASE_URL_VAR):
        ReadOnlyRest(url, ANON_KEY)


# ---- source scan: fails if a service key or a write method is ever added -------------------------

# Every academy module that mentions Supabase is scanned, so a future data file cannot dodge the rules.
DATA_SOURCES = sorted(p for p in ACADEMY.glob("*.py") if "supabase" in p.read_text(encoding="utf-8").lower())
WRITE_CALLS = {"post", "put", "patch", "delete", "request", "send", "stream", "upsert", "insert", "update", "rpc"}
FORBIDDEN_WORDS = ("service_role", "service-role", "sb_secret", "SERVICE_KEY", "SERVICE_ROLE", "SUPABASE_KEY", "SUPABASE_SERVICE")
ALLOWED_VARS = {settings.SUPABASE_URL_VAR, settings.SUPABASE_ANON_KEY_VAR}


@pytest.mark.parametrize("path", DATA_SOURCES, ids=lambda p: p.name)
def test_the_data_code_never_names_a_service_key(path):
    text = path.read_text(encoding="utf-8")
    assert not [w for w in FORBIDDEN_WORDS if w.lower() in text.lower()]


@pytest.mark.parametrize("path", DATA_SOURCES, ids=lambda p: p.name)
def test_the_data_code_never_calls_a_write_method(path):
    calls = [node.func.attr for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not (set(calls) & WRITE_CALLS), set(calls) & WRITE_CALLS


def test_the_data_code_reads_no_supabase_setting_except_url_and_anon_key():
    names = set()
    for path in DATA_SOURCES + [ACADEMY / "settings.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*SUPABASE[A-Z0-9_]*", node.value):
                names.add(node.value)
    assert names == ALLOWED_VARS


def test_the_scan_finds_the_data_files():
    """If this list were empty the scans above would pass without checking anything."""
    assert {"data_client.py", "data_tools.py"} <= {p.name for p in DATA_SOURCES}


# ---- the SQL file --------------------------------------------------------------------------------

SQL_CODE = "\n".join(line.split("--")[0] for line in SQL.splitlines())  # without comments


def test_every_table_has_row_level_security_and_a_select_only_policy():
    tables = re.findall(r"create table (\w+)", SQL_CODE)
    assert sorted(tables) == sorted(data_client.READABLE_TABLES)
    for table in tables:
        assert f"alter table {table} enable row level security" in SQL_CODE
    policies = re.findall(r'create policy "[^"]+" on (\w+) for (\w+) to (\w+)', SQL_CODE)
    assert sorted(policies) == sorted((t, "select", "anon") for t in tables)


def test_the_sql_gives_no_write_policy_and_no_write_privilege():
    assert not re.search(r"for\s+(insert|update|delete|all)\b", SQL_CODE, flags=re.I)
    grants = re.findall(r"grant\s+(.+?)\s+on\b", SQL_CODE, flags=re.I)
    assert grants == ["select"]
    assert "revoke all on" in SQL_CODE
    assert not re.search(r"\b(drop|truncate|alter\s+table\s+\w+\s+disable)\b", SQL_CODE, flags=re.I)  # never destroys or opens up


def test_the_sql_warns_to_run_only_in_the_test_project():
    assert "NEVER run it in the real EduChess project" in SQL


def test_the_seed_rows_in_the_sql_file_are_exactly_the_rows_in_config():
    assert seed_block(SQL) == render_seed(), "run: python -m email_assistant.academy.seed_sql --write"


def test_the_seed_block_covers_every_value_in_config():
    block = seed_block(SQL)
    for rows in (config.PLANS, config.BATCHES, config.WORKSHOPS, config.TOURNAMENTS):
        for row in rows:
            for value in row.values():
                assert (f"'{value}'" if isinstance(value, str) else str(value)) in block


def test_every_column_the_tools_select_exists_in_the_table():
    for tool_name, table in [("get_plans", "plans"), ("get_batch_timings", "batches"),
                             ("get_workshops", "workshops"), ("get_tournaments", "tournaments")]:
        server = SeedServer()
        with use_data_client(ReadOnlyRest(TEST_URL, ANON_KEY, transport=server.transport)):
            call(tool_name)
        selected = server.requests[0].url.params["select"].split(",")
        table_sql = re.search(rf"create table {table} \((.*?)\n\);", SQL_CODE, flags=re.S).group(1)
        columns = {line.split()[0] for line in table_sql.splitlines() if line.strip()}
        assert set(selected) <= columns, (table, set(selected) - columns)
