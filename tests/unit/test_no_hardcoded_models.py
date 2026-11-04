"""Guard: model and provider names live in env vars, never in Python code."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_FOR_INIT = {ROOT / "src" / "email_assistant" / "models.py"}
PROVIDER_LITERAL = re.compile(r"""["'](openai|google_genai|groq|anthropic|google_vertexai):""")


def _python_files():
    for folder in ("src", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if path != Path(__file__).resolve():
                yield path


def test_only_models_py_builds_models():
    offenders = [p.relative_to(ROOT) for p in _python_files()
                 if p not in ALLOWED_FOR_INIT and "init_chat_model(" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_no_provider_prefixed_model_strings():
    offenders = [p.relative_to(ROOT) for p in _python_files()
                 if PROVIDER_LITERAL.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
