"""Phase 4: a read-only MCP server, and the agent using it when USE_MCP=1. The results must not change."""

import asyncio
import json
import os
import sys
import threading

import pytest
from mcp import Client, StdioServerParameters

from academy_helpers import Run, triage_says
from email_assistant.academy import mcp_server, settings, tools
from email_assistant.academy.guards import READ_ONLY_TOOLS
from email_assistant.academy.mcp_bridge import (
    SERVER_ENV_VARS, McpError, McpReadClient, in_process_client, mcp_proxy_tools, stdio_target, use_mcp_client,
)
from email_assistant.academy.mcp_server import build_server
from email_assistant.academy.tools import READ_ONLY_TOOL_OBJECTS, WRITE_TOOLS, academy_tools
from email_assistant.models import use_fake_models
from fakes import ScriptedModel, tool_call_message

WRITE_NAMES = {t.name for t in WRITE_TOOLS}


@pytest.fixture
def client():
    client = in_process_client()
    yield client
    client.close()


class Spy(McpReadClient):
    """An in-process client that remembers which tools were called through it."""

    def __init__(self):
        super().__init__(build_server())
        self.called: list[str] = []

    def call(self, name, arguments=None):
        self.called.append(name)
        return super().call(name, arguments)


@pytest.fixture
def spy():
    spy = Spy()
    with use_mcp_client(spy):
        yield spy
    spy.close()


def listed_tools():
    async def listing():
        async with Client(build_server()) as c:
            return (await c.list_tools()).tools
    return asyncio.run(listing())


# ---- what the server offers ----------------------------------------------------------------------

def test_the_server_offers_exactly_the_read_only_tools(client):
    assert set(client.list_tool_names()) == set(READ_ONLY_TOOLS)
    assert len(client.list_tool_names()) == len(READ_ONLY_TOOLS)


def test_no_write_tool_is_offered_and_none_is_on_the_allow_list(client):
    assert not (set(client.list_tool_names()) & WRITE_NAMES) and WRITE_NAMES
    assert not (WRITE_NAMES & READ_ONLY_TOOLS)
    assert {"create_demo_event", "reschedule_demo", "cancel_demo", "send_reply"} <= WRITE_NAMES


def test_every_offered_tool_is_marked_read_only():
    assert all(t.annotations and t.annotations.read_only_hint for t in listed_tools())


def test_the_server_refuses_to_offer_a_tool_that_is_not_on_the_allow_list(monkeypatch):
    monkeypatch.setattr(mcp_server, "READ_ONLY_TOOL_OBJECTS", [*READ_ONLY_TOOL_OBJECTS, tools.send_reply])
    with pytest.raises(RuntimeError, match="send_reply"):
        build_server()


def test_names_descriptions_and_arguments_match_the_in_process_tools():
    by_name = {t.name: t for t in listed_tools()}
    for local in READ_ONLY_TOOL_OBJECTS:
        remote = by_name[local.name]
        assert remote.description == local.description
        assert set(remote.input_schema.get("properties", {})) == set(local.args_schema.model_json_schema().get("properties", {}))
        assert set(remote.input_schema.get("required", [])) == set(local.args_schema.model_json_schema().get("required", []))


# ---- the client side refuses writes too ----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(WRITE_NAMES) + ["Question", "wipe_database"])
def test_the_client_will_not_call_anything_off_the_allow_list(client, name):
    with pytest.raises(McpError, match="not a read-only tool"):
        client.call(name, {})


def test_a_write_tool_cannot_be_turned_into_an_mcp_proxy():
    with pytest.raises(McpError, match="send_reply"):
        mcp_proxy_tools([tools.send_reply])


# ---- same answers both ways ----------------------------------------------------------------------

@pytest.mark.parametrize("args_for", [
    ("find_demo_slots", {"date_from": "2026-11-04", "date_to": "2026-11-09"}),
    ("get_plans", {}), ("get_batch_timings", {}), ("get_workshops", {}), ("get_tournaments", {}),
])
def test_each_tool_gives_the_same_answer_directly_and_over_mcp(academy_world, spy, args_for):
    name, arguments = args_for
    direct = next(t for t in READ_ONLY_TOOL_OBJECTS if t.name == name)
    proxy = next(t for t in mcp_proxy_tools(READ_ONLY_TOOL_OBJECTS) if t.name == name)
    assert proxy.invoke(arguments) == direct.invoke(arguments)
    assert spy.called == [name]  # and the proxy really went through the MCP client


def test_a_tool_error_travels_over_mcp_with_the_variable_name(client, monkeypatch):
    from email_assistant.academy import data_client
    monkeypatch.setattr(data_client, "_override", None)  # no test client: the tool must build one from missing settings
    monkeypatch.setattr(data_client, "_cached", None)
    with use_mcp_client(client):
        proxy = mcp_proxy_tools([next(t for t in READ_ONLY_TOOL_OBJECTS if t.name == "get_plans")])[0]
        with pytest.raises(McpError, match=settings.SUPABASE_URL_VAR):
            proxy.invoke({})


def test_calls_from_several_threads_all_get_their_own_answer(academy_world, client):
    results = {}

    def work(i):
        results[i] = client.call("find_demo_slots", {"date_from": "2026-11-0%d" % (4 + i % 3), "date_to": "2026-11-09"})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert len(results) == 8 and all(json.loads(r)["source"] for r in results.values())


# ---- USE_MCP in the graph ------------------------------------------------------------------------

def test_use_mcp_swaps_only_the_read_only_tools(monkeypatch):
    monkeypatch.delenv(settings.USE_MCP_VAR, raising=False)
    direct = {t.name: t for t in academy_tools()}
    monkeypatch.setenv(settings.USE_MCP_VAR, "1")
    swapped = {t.name: t for t in academy_tools()}
    assert set(direct) == set(swapped)
    for name in READ_ONLY_TOOLS:
        assert swapped[name] is not direct[name] and swapped[name].description == direct[name].description
    for name in set(direct) - READ_ONLY_TOOLS:
        assert swapped[name] is direct[name]  # write tools and signals stay in-process


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("no", False)])
def test_use_mcp_reads_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv(settings.USE_MCP_VAR, value)
    assert settings.use_mcp() is expected


def _run_flow(mode, monkeypatch):
    """get_plans, then book a demo, then send a reply; the owner accepts every write. Returns the tool messages."""
    monkeypatch.setenv(settings.USE_MCP_VAR, "1" if mode == "mcp" else "0")
    agent = ScriptedModel(
        tool_call_message("get_plans"),
        tool_call_message("find_demo_slots", date_from="2026-11-04", date_to="2026-11-09"),
        tool_call_message("create_demo_event", parent_email="parent.a@example.invalid", child_first_name="Arjun",
                          start="2026-11-07T10:00", end="2026-11-07T10:45"),
        tool_call_message("send_reply", to="parent.a@example.invalid", subject="Demo", body="Booked."),
        tool_call_message("Done", done=True),
    )
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        asked = []
        result = run.start()
        while run.pending(result):
            asked.append(run.pending(result)["action_request"]["action"])
            result = run.answer(type="accept")
    return asked, [m.content for m in run.state.values["messages"] if m.type == "tool"]


def test_the_whole_booking_flow_gives_identical_results_both_ways(academy_world, spy, monkeypatch):
    direct_asked, direct_tools = _run_flow("direct", monkeypatch)
    assert spy.called == []  # not through MCP
    academy_world.calendar.events.clear()
    mcp_asked, mcp_tools = _run_flow("mcp", monkeypatch)
    assert spy.called == ["get_plans", "find_demo_slots"]  # read-only tools went through MCP
    assert direct_asked == mcp_asked == ["create_demo_event", "send_reply"]  # writes still ask the owner
    strip = lambda items: [json.loads(i) | {"outbox_id": None, "event_id": None} for i in items]  # noqa: E731
    assert strip(direct_tools) == strip(mcp_tools)


def test_writes_stay_in_process_and_wait_for_the_owner_when_mcp_is_on(academy_world, spy, monkeypatch):
    monkeypatch.setenv(settings.USE_MCP_VAR, "1")
    agent = ScriptedModel(tool_call_message("create_demo_event", parent_email="p@example.invalid", child_first_name="Arjun",
                                            start="2026-11-07T10:00", end="2026-11-07T10:45"), tool_call_message("Done", done=True))
    with use_fake_models(triage=triage_says("book_demo"), agent=agent):
        run = Run()
        assert run.pending(run.start())["action_request"]["action"] == "create_demo_event"
        assert academy_world.calendar.events == {} and spy.called == []
        run.answer(type="accept")
    assert len(academy_world.calendar.events) == 1 and spy.called == []


# ---- the real server as a child process ----------------------------------------------------------

def test_the_child_process_gets_only_the_settings_it_needs(monkeypatch):
    for name, value in {"GROQ_API_KEY": "gsk_secret", "LANGSMITH_API_KEY": "ls_secret", "GOOGLE_API_KEY": "g_secret",
                        settings.SUPABASE_URL_VAR: "https://x.supabase.co", settings.SUPABASE_ANON_KEY_VAR: "anon",
                        settings.CALENDAR_ID_VAR: "cal"}.items():
        monkeypatch.setenv(name, value)
    target = stdio_target()
    assert set(target.env) == {settings.SUPABASE_URL_VAR, settings.SUPABASE_ANON_KEY_VAR, settings.CALENDAR_ID_VAR}
    assert set(target.env) <= set(SERVER_ENV_VARS) and "secret" not in str(target.env)
    assert (target.command, target.args) == (sys.executable, ["-m", "email_assistant.academy.mcp_server"])
    assert target.cwd == os.getcwd()


def test_the_real_server_runs_as_a_child_process_and_offers_the_same_tools():
    client = McpReadClient(stdio_target(), timeout=60)
    try:
        assert set(client.list_tool_names()) == set(READ_ONLY_TOOLS)
        with pytest.raises(McpError, match=settings.SUPABASE_URL_VAR):  # no settings in the child: a clear error comes back
            client.call("get_plans", {})
    finally:
        client.close()


def test_a_server_that_cannot_start_gives_an_error_not_a_hang():
    broken = McpReadClient(StdioServerParameters(command="this-command-does-not-exist-anywhere", args=[]), timeout=20)
    with pytest.raises(McpError, match="could not be started"):
        broken.call("get_plans", {})


def test_close_stops_the_background_thread(academy_world):
    client = in_process_client()
    client.call("get_plans", {})
    thread = client._thread
    assert thread.is_alive()
    client.close()
    assert not thread.is_alive()
