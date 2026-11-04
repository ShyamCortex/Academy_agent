"""The first-time Google sign-in command. The browser step is replaced; nothing here touches Google."""

import json
import stat
import sys

import pytest

from email_assistant.academy import google_auth, settings


@pytest.fixture
def files(monkeypatch, tmp_path):
    credentials, token = tmp_path / "creds.json", tmp_path / "sub" / "token.json"
    monkeypatch.setenv(settings.CREDENTIALS_FILE_VAR, str(credentials))
    monkeypatch.setenv(settings.TOKEN_FILE_VAR, str(token))
    monkeypatch.chdir(tmp_path)  # so a stray .env in the repo is not read
    return credentials, token


def test_no_client_file_gives_step_by_step_help_and_opens_no_browser(files, capsys):
    credentials, _ = files
    assert google_auth.main([]) == 1
    out = capsys.readouterr().out
    assert str(credentials) in out and "LAPTOP_SETUP" in out


def test_sign_in_saves_the_token_where_the_tools_look_for_it(files, monkeypatch, capsys):
    credentials, token = files
    credentials.write_text("{}")

    class FakeCreds:
        def to_json(self):
            return json.dumps({"refresh_token": "SECRET-REFRESH", "token": "SECRET-ACCESS"})

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert path == str(credentials) and scopes == ["https://www.googleapis.com/auth/calendar.events"]
            return cls()

        def run_local_server(self, **kwargs):
            assert kwargs["port"] == 0  # any free port
            return FakeCreds()

    import google_auth_oauthlib.flow as flow
    monkeypatch.setattr(flow, "InstalledAppFlow", FakeFlow)
    assert google_auth.sign_in() == 0
    assert json.loads(token.read_text())["refresh_token"] == "SECRET-REFRESH"
    if sys.platform != "win32":
        assert stat.S_IMODE(token.stat().st_mode) == 0o600  # only the owner can read it
    out = capsys.readouterr().out
    assert "SECRET" not in out and str(token) in out  # the token is never printed


def test_check_says_what_to_do_when_there_is_no_sign_in(files, monkeypatch, capsys):
    monkeypatch.setenv(settings.CALENDAR_ID_VAR, "cal@group.calendar.google.com")
    assert google_auth.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "google_auth" in out and "Traceback" not in out


def test_check_names_the_missing_calendar_id(files, monkeypatch, capsys):
    monkeypatch.delenv(settings.CALENDAR_ID_VAR, raising=False)
    assert google_auth.main(["--check"]) == 1
    assert settings.CALENDAR_ID_VAR in capsys.readouterr().out
