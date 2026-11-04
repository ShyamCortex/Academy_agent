"""`model_check --list` used to fail for Google with "RuntimeError: Cannot send a request, as the client has been closed"."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from email_assistant import model_check


class StubGoogle(BaseHTTPRequestHandler):
    """Answers the model-list request the way the Gemini API does, on a port on this computer."""

    def do_GET(self):  # noqa: N802
        body = json.dumps({"models": [
            {"name": "models/gemini-test-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-test-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-test", "supportedGenerationMethods": ["embedContent"]},
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def google_stub(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), StubGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-a-local-stub")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    yield server
    server.shutdown()


def test_the_google_listing_works_with_the_real_google_client(google_stub):
    """The real google-genai client talks to a local stub. Before the fix this raised the RuntimeError above."""
    assert model_check.google_model_ids() == ["gemini-test-flash", "gemini-test-lite"]  # no embedding model


def test_list_prints_both_providers_and_one_failing_does_not_hide_the_other(monkeypatch, capsys):
    monkeypatch.setattr(model_check, "google_model_ids", lambda: (_ for _ in ()).throw(RuntimeError("boom with key-123")))
    monkeypatch.setattr(model_check, "groq_model_ids", lambda: ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"])
    monkeypatch.setenv("GOOGLE_API_KEY", "key-123")
    model_check.list_models()
    out = capsys.readouterr().out
    assert "Google: could not list models: RuntimeError" in out and "key-123" not in out  # the key is scrubbed
    assert "Groq (use as groq:<id>):" in out and "openai/gpt-oss-120b" in out
