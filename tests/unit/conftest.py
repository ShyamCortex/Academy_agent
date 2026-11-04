"""Every unit test gets a fake world: a fake calendar, a fake Supabase, a fixed clock, a temp outbox.

So no unit test can reach the network or use a real key. Run the whole suite with USE_MCP=1 to send
the read-only tools through an in-process MCP server instead; the tests must pass both ways.
"""

from types import SimpleNamespace

import pytest

from academy_helpers import ANON_KEY, NOW, TEST_URL, SeedServer
from email_assistant.academy import dates, settings
from email_assistant.academy.calendar_backend import FakeCalendar, use_calendar
from email_assistant.academy.data_client import ReadOnlyRest, use_data_client
from email_assistant.academy.mcp_bridge import in_process_client, use_mcp_client


@pytest.fixture(scope="session")
def mcp_client():
    client = in_process_client()
    yield client
    client.close()


@pytest.fixture(autouse=True)
def academy_world(monkeypatch, tmp_path, request):
    monkeypatch.setattr(dates, "now_ist", lambda: NOW)
    monkeypatch.setenv(settings.OUTBOX_FILE_VAR, str(tmp_path / "outbox.jsonl"))
    for name in (settings.SUPABASE_URL_VAR, settings.SUPABASE_ANON_KEY_VAR, settings.CALENDAR_ID_VAR):
        monkeypatch.delenv(name, raising=False)  # a developer's real .env values must never leak into a test
    calendar, server = FakeCalendar(), SeedServer()
    client = ReadOnlyRest(TEST_URL, ANON_KEY, transport=server.transport, sleep=lambda s: None)
    with use_calendar(calendar), use_data_client(client):
        if settings.use_mcp():
            with use_mcp_client(request.getfixturevalue("mcp_client")):
                yield SimpleNamespace(calendar=calendar, server=server, now=NOW, outbox=tmp_path / "outbox.jsonl")
        else:
            yield SimpleNamespace(calendar=calendar, server=server, now=NOW, outbox=tmp_path / "outbox.jsonl")
