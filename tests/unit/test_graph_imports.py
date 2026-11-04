"""Every graph module must import with NO API keys and NO model env vars.

Each import runs in a fresh Python process in an empty folder, so a leftover .env or an
already-imported module cannot hide a problem.
"""

import subprocess
import sys

import pytest

from academy_helpers import clean_env

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


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_without_keys(module, tmp_path):
    # A clean environment in an empty folder: no key, no model name and no `.env` can reach the import.
    result = subprocess.run(
        [sys.executable, "-c", f"import email_assistant.{module}"],
        cwd=tmp_path, env=clean_env(LANGSMITH_TRACING="false"), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
