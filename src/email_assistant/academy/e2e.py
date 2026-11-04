"""One end-to-end run on your laptop: a booking message -> you approve in LangGraph Studio -> check the results.

    1. In one terminal:   uv run langgraph dev
    2. In another:        uv run python -m email_assistant.academy.e2e

The command sends the sample message `SAMPLE-04` (a `book_demo` message) to the running graph, then
waits while you approve each step in LangGraph Studio. When the run ends it checks two things:
the demo event is in the TEST calendar, and the reply is in the local outbox file.

It prints action names only, never message text (hard rule 5).
"""

import argparse
import os
import sys
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from email_assistant.academy import dates, settings
from email_assistant.academy.calendar_backend import get_calendar, token_path
from email_assistant.academy.dataset import Message, load_messages
from email_assistant.academy.outbox import outbox_path, read_outbox

GRAPH_ID = "academy_agent"
SAMPLES = Path("data/eval/sample_messages.jsonl")
DEFAULT_URL = "http://127.0.0.1:2024"
RESUME_HELP = 'In Studio, open the thread, read the request, and resume with:  [{"type": "accept"}]'


def missing_settings() -> list[str]:
    """Settings this command and the graph need that are not in place yet."""
    needed = ["TRIAGE_MODEL", "AGENT_MODEL", settings.SUPABASE_URL_VAR, settings.SUPABASE_ANON_KEY_VAR, settings.CALENDAR_ID_VAR]
    missing = [name for name in needed if not os.environ.get(name, "").strip()]
    if not token_path().exists():
        missing.append(f"Google sign-in file {token_path()} (run: python -m email_assistant.academy.google_auth)")
    return missing


def pending_actions(thread: dict[str, Any]) -> list[str]:
    """Names of the actions waiting for the owner, from a thread's interrupts. Names only, no arguments."""
    names = []
    for interrupts in (thread.get("interrupts") or {}).values():
        for interrupt in interrupts:
            value = interrupt.get("value")
            request = value[0] if isinstance(value, list) and value else value
            action = (request or {}).get("action_request", {}).get("action") if isinstance(request, dict) else None
            names.append(action or "a request")
    return names


def wait_for_finish(
    client: Any, thread_id: str, *, timeout: float, poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    say: Callable[[str], None] = print,
) -> str:
    """Wait until the thread is not busy and has nothing waiting for the owner. Returns "idle", "error" or "timeout"."""
    deadline = clock() + timeout
    announced: set[str] = set()
    while clock() < deadline:
        thread = client.threads.get(thread_id)
        status = thread.get("status")
        if status == "idle" or status == "error":
            return status
        if status == "interrupted":
            for name in pending_actions(thread):
                key = f"{name}:{sorted((thread.get('interrupts') or {}))}"
                if key not in announced:
                    announced.add(key)
                    say(f"  Waiting for you in Studio: {name}")
        sleep(poll)
    return "timeout"


def parent_address(message: Message) -> str:
    return parseaddr(message.sender)[1]


def verify(message: Message, child_first_name: str, outbox_before: int, say: Callable[[str], None] = print) -> bool:
    """Check the two results. Prints PASS or FAIL for each and returns True only if both pass."""
    address = parent_address(message)
    queued = [r for r in read_outbox()[outbox_before:] if r.get("to") == address]
    ok_outbox = bool(queued)
    say(f"{'PASS' if ok_outbox else 'FAIL'}  reply in the outbox ({outbox_path()}): {len(queued)} new reply to the parent")

    events = get_calendar().find_demos(address, child_first_name, dates.now_ist())
    ok_calendar = len(events) == 1
    when = dates.iso_minutes(events[0].start) if events else "-"
    say(f"{'PASS' if ok_calendar else 'FAIL'}  demo event in the test calendar: {len(events)} found, start {when}")
    return ok_outbox and ok_calendar


def run(args: argparse.Namespace, client: Any | None = None, say: Callable[[str], None] = print) -> int:
    missing = missing_settings()
    if missing:
        say("Not ready. Missing:")
        for item in missing:
            say(f"  - {item}")
        say("See docs/agent/LAPTOP_SETUP.md.")
        return 1
    rows = {m.id: m for m in load_messages(SAMPLES)}
    message = rows.get(args.sample)
    if message is None or message.expected_label != "book_demo":
        say(f"{args.sample!r} is not a book_demo sample in {SAMPLES}.")
        return 1

    if client is None:
        from langgraph_sdk import get_sync_client

        client = get_sync_client(url=args.url)
    try:
        thread = client.threads.create()
    except Exception as exc:
        say(f"Could not reach the graph server at {args.url} ({type(exc).__name__}). Start it first with:  uv run langgraph dev")
        return 1
    thread_id = thread["thread_id"]
    outbox_before = len(read_outbox())

    run_info = client.runs.create(thread_id, GRAPH_ID, input={"message_input": message.graph_input()})
    say(f"Sent {message.id} to the '{GRAPH_ID}' graph. Thread id: {thread_id}")
    say(f"Open LangGraph Studio (the link `langgraph dev` printed, with baseUrl={args.url}) and pick this thread.")
    say(RESUME_HELP)
    say("Expect two requests: create_demo_event, then send_reply. Approve both.")
    client.runs.join(thread_id, run_info["run_id"])  # returns when the first stretch ends (at the first request)

    outcome = wait_for_finish(client, thread_id, timeout=args.timeout, say=say)
    if outcome != "idle":
        say(f"The run ended with: {outcome}. Nothing was checked.")
        return 1
    say("The run is finished. Checking the results:")
    return 0 if verify(message, args.child, outbox_before, say) else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Send a booking sample to a running `langgraph dev` and check the results.")
    parser.add_argument("--sample", default="SAMPLE-04", help="id of a book_demo row in data/eval/sample_messages.jsonl")
    parser.add_argument("--child", default="Arjun", help="the child's first name in that sample")
    parser.add_argument("--url", default=DEFAULT_URL, help="where `langgraph dev` is listening")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for your approvals")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
