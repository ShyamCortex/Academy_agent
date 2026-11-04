"""Every graph module must import with NO API keys and NO model env vars.

Each import runs in a fresh Python process in an empty folder, so a leftover .env or an
already-imported module cannot hide a problem.
"""

import os
import subprocess
import sys

import pytest

MODULES = [
    "langgraph_101",
    "email_assistant",
    "email_assistant_hitl",
    "email_assistant_hitl_memory",
    "email_assistant_hitl_memory_gmail",
    "academy.graph",
    "academy.triage_accuracy",
    "academy.dataset",
]
SECRET_AND_MODEL_VARS = [
    "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "LANGSMITH_API_KEY",
    "TRIAGE_MODEL", "AGENT_MODEL", "JUDGE_MODEL",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_without_keys(module, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_AND_MODEL_VARS}
    env["LANGSMITH_TRACING"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", f"import email_assistant.{module}"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
