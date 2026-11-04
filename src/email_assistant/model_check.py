"""Check the model setup on your own machine (uses your real keys).

    python -m email_assistant.model_check            show which env vars are set (never the key values)
    python -m email_assistant.model_check --list     list the model ids your keys can use
    python -m email_assistant.model_check --ping     make a few tiny real calls and report PASS / FAIL

`--ping` tests the exact things the graphs depend on: structured output (triage and
memory), forced tool calls (`tool_choice="required"` and `"any"`), and the judge.
It uses only made-up text. Run it before the long test suite.
"""

import argparse
import os
import sys
import time
from typing import Callable

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from email_assistant.models import ROLE_ENV_VARS, structured_llm, tool_llm
from email_assistant.schemas import RouterSchema, UserPreferences
from email_assistant.tools import get_tools

# Environment variables that hold secrets. We only ever report "set" or "missing".
KEY_VARS = ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "LANGSMITH_API_KEY"]


class Grade(BaseModel):
    """Tiny schema to check that the judge model can return structured output."""

    grade: bool = Field(description="Is the answer correct?")
    justification: str = Field(description="One sentence why.")


def _scrub(text: str) -> str:
    """Remove any secret value from text before printing it."""
    for var in KEY_VARS:
        value = os.environ.get(var)
        if value:
            text = text.replace(value, "***")
    return text


def show_config() -> None:
    print("Model roles (from .env):")
    for role, var in ROLE_ENV_VARS.items():
        print(f"  {role:7} {var:13} {os.environ.get(var, '').strip() or 'NOT SET'}")
    print("Keys (values are never shown):")
    for var in KEY_VARS:
        print(f"  {var:18} {'set' if os.environ.get(var) else 'missing'}")


def google_model_ids() -> list[str]:
    """Gemini model ids this key can use for text generation.

    The client must stay open while the list is read: `models.list()` returns a lazy pager that fetches
    pages as you loop, and a client that is no longer referenced closes its HTTP connection. (Writing
    `genai.Client().models.list()` freed the client at once and failed with "Cannot send a request, as
    the client has been closed".) So the client lives in a `with` block and the pager is read inside it.
    """
    from google import genai

    with genai.Client() as client:  # reads GOOGLE_API_KEY / GEMINI_API_KEY itself
        return sorted(
            m.name.removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or []) and "gemini" in m.name
        )


def groq_model_ids() -> list[str]:
    """Groq model ids this key can use."""
    from groq import Groq

    with Groq() as client:  # reads GROQ_API_KEY itself
        return sorted(m.id for m in client.models.list().data)


def list_models() -> None:
    """Print the model ids each provider says your key can use."""
    for title, fetch in (("Google (use as google_genai:<id>)", google_model_ids), ("Groq (use as groq:<id>)", groq_model_ids)):
        try:
            ids = fetch()
        except Exception as exc:
            print(f"{title.split(' (')[0]}: could not list models: {type(exc).__name__}: {_scrub(str(exc))[:200]}")
            continue
        print(f"{title}:")
        print("  " + "\n  ".join(ids))


def _check(label: str, fn: Callable[[], str]) -> bool:
    start = time.time()
    try:
        detail = fn()
    except Exception as exc:
        print(f"FAIL {label}: {type(exc).__name__}: {_scrub(str(exc))[:300]}")
        return False
    print(f"PASS {label} ({time.time() - start:.1f}s) {detail}")
    return True


def ping() -> bool:
    """Make a few tiny real calls. Returns True only if all pass."""
    tools = get_tools(["write_email", "check_calendar_availability", "Question", "Done"])
    ask = [
        {"role": "system", "content": "You are an assistant. Always use a tool."},
        {"role": "user", "content": "Reply to this email: 'Hi, are you free Tuesday at 3pm for a call? - Sam'"},
    ]

    def triage() -> str:
        out = structured_llm("triage", RouterSchema).invoke([
            {"role": "system", "content": "Classify the email as ignore, notify or respond."},
            {"role": "user", "content": "Subject: Lunch?\nHi, are you free Tuesday for lunch? - Sam"},
        ])
        return f"classification={out.classification}"

    def tools_call(choice: str) -> Callable[[], str]:
        def run() -> str:
            message = tool_llm("agent", tools, tool_choice=choice).invoke(ask)
            names = [call["name"] for call in message.tool_calls]
            assert names, f"no tool call came back although tool_choice={choice!r}"
            return f"tool_calls={names}"
        return run

    def memory() -> str:
        out = structured_llm("agent", UserPreferences).invoke([
            {"role": "system", "content": "Update the user's preferences profile from the feedback."},
            {"role": "user", "content": "Current profile: 'Be brief.' Feedback: 'Always sign off with Thanks.'"},
        ])
        return f"profile={out.user_preferences[:60]!r}"

    def judge() -> str:
        out = structured_llm("judge", Grade).invoke([
            {"role": "system", "content": "Grade the answer."},
            {"role": "user", "content": "Question: 2+2? Answer: 4. Is the answer correct?"},
        ])
        return f"grade={out.grade}"

    results = [
        _check("triage  structured output (TRIAGE_MODEL)", triage),
        _check("agent   forced tool call, tool_choice='required' (AGENT_MODEL)", tools_call("required")),
        _check("agent   forced tool call, tool_choice='any' (AGENT_MODEL)", tools_call("any")),
        _check("agent   structured output for memory (AGENT_MODEL)", memory),
        _check("judge   structured output (JUDGE_MODEL)", judge),
    ]
    print("ALL PASS" if all(results) else "SOME CHECKS FAILED")
    return all(results)


def main() -> int:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Check the Gemini / Groq model setup.")
    parser.add_argument("--list", action="store_true", help="list model ids your keys can use")
    parser.add_argument("--ping", action="store_true", help="make tiny real calls and report PASS/FAIL")
    args = parser.parse_args()

    show_config()
    if args.list:
        print()
        list_models()
    if args.ping:
        print()
        return 0 if ping() else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
