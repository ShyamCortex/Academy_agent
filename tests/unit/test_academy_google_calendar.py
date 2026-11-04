"""GoogleCalendar against Google's own client library, with the HTTP layer replaced by a recorder.

The real `googleapiclient` builds the requests (from its bundled copy of the Calendar API description),
so these tests check what would really be sent. No network, no keys.
"""

import json
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from email_assistant.academy import config, settings
from email_assistant.academy.calendar_backend import (
    SCOPES, CalendarError, GoogleCalendar, load_credentials, parent_key,
)

CAL = "test-calendar@group.calendar.google.com"
PARENT = "parent.a@example.invalid"
IST = config.TIMEZONE


def google(*responses):
    """A GoogleCalendar whose HTTP answers are `responses` ((status, body) pairs). Returns (calendar, http)."""
    http = HttpMockSequence([({"status": str(status)}, json.dumps(body)) for status, body in responses])
    service = build("calendar", "v3", http=http, static_discovery=True, cache_discovery=False)
    return GoogleCalendar(CAL, service), http


def sent(http, index=0):
    uri, method, body, _ = http.request_sequence[index]
    parts = urlsplit(uri)
    return method, unquote(parts.path), parse_qs(parts.query), json.loads(body) if body else None


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def test_the_only_permission_asked_for_is_calendar_events():
    assert SCOPES == ["https://www.googleapis.com/auth/calendar.events"]


def test_busy_reads_timed_and_all_day_events_and_skips_cancelled_and_free_ones():
    calendar, http = google((200, {"items": [
        {"id": "a", "start": {"dateTime": "2026-11-07T10:00:00+05:30"}, "end": {"dateTime": "2026-11-07T10:45:00+05:30"}},
        {"id": "b", "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}},  # an all-day event
        {"id": "c", "status": "cancelled", "start": {"dateTime": "2026-11-07T11:00:00+05:30"}, "end": {"dateTime": "2026-11-07T12:00:00+05:30"}},
        {"id": "d", "transparency": "transparent", "start": {"dateTime": "2026-11-07T13:00:00+05:30"}, "end": {"dateTime": "2026-11-07T14:00:00+05:30"}},
    ]}))
    blocks = calendar.busy(at("2026-11-07T00:00"), at("2026-11-10T00:00"))
    assert [(b.event_id, b.start, b.end) for b in blocks] == [
        ("a", at("2026-11-07T10:00"), at("2026-11-07T10:45")),
        ("b", at("2026-11-09T00:00"), at("2026-11-10T00:00")),
    ]
    method, path, query, _ = sent(http)
    assert (method, path) == ("GET", f"/calendar/v3/calendars/{CAL}/events")
    assert query["singleEvents"] == ["true"] and query["timeMin"] and query["timeMax"]


def test_busy_follows_pages():
    calendar, http = google((200, {"items": [], "nextPageToken": "p2"}), (200, {"items": []}))
    calendar.busy(at("2026-11-07T00:00"), at("2026-11-08T00:00"))
    assert len(http.request_sequence) == 2 and sent(http, 1)[2]["pageToken"] == ["p2"]


def test_a_new_event_is_sent_to_the_named_calendar_in_india_time_and_nobody_is_emailed():
    calendar, http = google((200, {"id": "evt123"}))
    event = calendar.create_demo("Demo class - Arjun", at("2026-11-07T10:00"), at("2026-11-07T10:45"), PARENT, "Arjun")
    assert event.event_id == "evt123"
    method, path, query, body = sent(http)
    assert (method, path) == ("POST", f"/calendar/v3/calendars/{CAL}/events")
    assert query["sendUpdates"] == ["none"]  # Google must not email anyone
    assert body["summary"] == "Demo class - Arjun"
    assert body["start"] == {"dateTime": "2026-11-07T10:00:00+05:30", "timeZone": "Asia/Kolkata"}
    assert body["end"] == {"dateTime": "2026-11-07T10:45:00+05:30", "timeZone": "Asia/Kolkata"}
    assert "attendees" not in body


def test_the_parents_email_never_leaves_the_machine_only_a_hash_does():
    calendar, http = google((200, {"id": "evt123"}))
    calendar.create_demo("Demo class - Arjun", at("2026-11-07T10:00"), at("2026-11-07T10:45"), PARENT, "Arjun")
    _, _, query, body = sent(http)
    private = body["extendedProperties"]["private"]
    assert private == {"academy_kind": "academy_demo", "academy_parent": parent_key(PARENT), "academy_child": "arjun"}
    assert PARENT not in json.dumps(body) and PARENT not in str(http.request_sequence)


def test_finding_a_demo_asks_google_to_match_both_hidden_labels():
    calendar, http = google((200, {"items": [
        {"id": "a", "start": {"dateTime": "2026-11-07T10:00:00+05:30"}, "end": {"dateTime": "2026-11-07T10:45:00+05:30"}},
        {"id": "gone", "status": "cancelled", "start": {"dateTime": "2026-11-09T10:00:00+05:30"}, "end": {"dateTime": "2026-11-09T10:45:00+05:30"}},
    ]}))
    found = calendar.find_demos(PARENT.upper(), "ARJUN", at("2026-11-04T10:00"))
    assert [e.event_id for e in found] == ["a"]
    assert sent(http)[2]["privateExtendedProperty"] == [f"academy_parent={parent_key(PARENT)}", "academy_child=arjun"]


def test_move_is_a_patch_of_the_times_only_and_delete_is_a_delete():
    calendar, http = google((200, {"id": "evt123"}), (204, ""))
    calendar.move_demo("evt123", at("2026-11-09T17:00"), at("2026-11-09T17:45"))
    method, path, query, body = sent(http, 0)
    assert (method, path) == ("PATCH", f"/calendar/v3/calendars/{CAL}/events/evt123")
    assert set(body) == {"start", "end"} and query["sendUpdates"] == ["none"]
    calendar.delete_demo("evt123")
    method, path, query, _ = sent(http, 1)
    assert (method, path) == ("DELETE", f"/calendar/v3/calendars/{CAL}/events/evt123") and query["sendUpdates"] == ["none"]


def test_a_google_error_becomes_a_short_calendar_error_without_details():
    calendar, _ = google((403, {"error": {"message": "secret details about the calendar"}}))
    with pytest.raises(CalendarError) as caught:
        calendar.busy(at("2026-11-07T00:00"), at("2026-11-08T00:00"))
    assert str(caught.value) == "Google Calendar answered with HTTP 403." and "secret" not in str(caught.value)


def test_no_sign_in_file_gives_a_helpful_error(monkeypatch, tmp_path):
    monkeypatch.setenv(settings.TOKEN_FILE_VAR, str(tmp_path / "nope.json"))
    with pytest.raises(settings.AcademyConfigError, match="google_auth") as caught:
        load_credentials()
    assert str(tmp_path / "nope.json") in str(caught.value)


def test_a_missing_calendar_id_names_the_variable(monkeypatch):
    monkeypatch.delenv(settings.CALENDAR_ID_VAR, raising=False)
    with pytest.raises(settings.AcademyConfigError, match=settings.CALENDAR_ID_VAR):
        GoogleCalendar.from_env()


def test_there_is_no_default_calendar():
    """We never fall back to a personal "primary" calendar."""
    import inspect

    from email_assistant.academy import calendar_backend
    assert '"primary"' not in inspect.getsource(calendar_backend)
