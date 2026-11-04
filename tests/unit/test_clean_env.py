"""`clean_env`: what a test's child process may inherit. No key, model name or academy setting from the developer's shell."""

from academy_helpers import clean_env


def test_secrets_and_settings_are_not_inherited(monkeypatch):
    for name in ("GROQ_API_KEY", "LANGSMITH_API_KEY", "TRIAGE_MODEL", "ACADEMY_SUPABASE_URL", "ACADEMY_SUPABASE_ANON_KEY", "GOOGLE_CALENDAR_ID", "USE_MCP"):
        monkeypatch.setenv(name, "from-the-shell")
    env = clean_env()
    assert not {"GROQ_API_KEY", "LANGSMITH_API_KEY", "TRIAGE_MODEL", "ACADEMY_SUPABASE_URL", "ACADEMY_SUPABASE_ANON_KEY", "GOOGLE_CALENDAR_ID", "USE_MCP"} & set(env)


def test_what_a_python_child_needs_to_start_is_inherited(monkeypatch):
    monkeypatch.setenv("PATH", "/some/bin")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("PYTHONPATH", "/some/src")
    assert clean_env()["PATH"] == "/some/bin" and clean_env()["SYSTEMROOT"] == "C:\\Windows" and clean_env()["PYTHONPATH"] == "/some/src"


def test_extra_values_are_added_and_win(monkeypatch):
    monkeypatch.setenv("TMP", "/from/shell")
    assert clean_env(LANGSMITH_TRACING="false", TMP="/mine") == {**clean_env(), "LANGSMITH_TRACING": "false", "TMP": "/mine"}
