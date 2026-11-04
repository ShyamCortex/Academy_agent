"""Run one message through the real academy graph with a scripted owner, and record a `Trace`.

The graph stops and waits for the owner at every send, every calendar change and every question
(hard rule 1). In an eval nobody is there, so a scripted owner answers each stop:

    a booking or a reply     accept (the draft is then run, exactly as in real use)
    a Question to the owner  a fixed answer: "no more information, use only what the tools returned"
    the sensitive notice     ignore (the run ends there whatever the owner answers)

If the agent keeps asking (more than MAX_REQUESTS stops) the run is cut off and marked unfinished.

Calendar: by default every message gets its own fresh `FakeCalendar` (in memory, the same every run). Nothing
reaches Google and no Google quota is used. If the message has an `existing_demo`, that booking is put in the fake
calendar first (`seed_calendar`), so a reschedule or cancel message has something to move. With `real_calendar=True`
(the CLI's --real-calendar) the real Google test calendar is used instead and nothing is seeded.
The CLI also points the outbox at an eval-only file. Data lookups still read the test Supabase project (anon key).

`UsageTracker` counts tokens per model role (triage, agent, judge) with a LangChain callback. It finds the role
from the name of the wrapper in `models.py` (`triage_structured_...`, `agent_tools`, `judge_structured_...`), so it
does not depend on which model or provider is behind it.
"""

import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any, Callable
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from email_assistant.academy import config, dates
from email_assistant.academy.calendar_backend import FakeCalendar, use_calendar
from email_assistant.academy.dataset import Message
from email_assistant.academy.eval_scorers import ROLES, SIGNAL_NAMES, ToolCall, Trace

MAX_REQUESTS = 12
QUESTION_ANSWER = "I have no more information. Use only what the tools returned. If you cannot answer, say the owner will reply personally."


def scripted_owner(request: dict) -> dict:
    """The owner's answer to one stop. `request` is the Agent-Inbox-style dict the graph interrupts with."""
    if request["action_request"]["action"] == "Question":
        return {"type": "response", "args": QUESTION_ANSWER}
    if request["config"].get("allow_accept"):
        return {"type": "accept"}
    return {"type": "ignore"}  # the sensitive notice: nothing can be accepted there


class UsageTracker(BaseCallbackHandler):
    """Adds up model calls and tokens per role. Pass it as `config={"callbacks": [tracker]}`."""

    def __init__(self) -> None:
        self._parent: dict[UUID, UUID | None] = {}
        self._role: dict[UUID, str] = {}
        self.totals: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs: Any) -> None:
        self._parent[run_id] = parent_run_id
        name = kwargs.get("name") or ""
        for role in ROLES:
            if name.startswith(f"{role}_"):
                self._role[run_id] = role

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs: Any) -> None:
        self._parent[run_id] = parent_run_id

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs: Any) -> None:
        self._parent[run_id] = parent_run_id

    def _role_of(self, run_id: UUID | None) -> str:
        while run_id is not None:
            if run_id in self._role:
                return self._role[run_id]
            run_id = self._parent.get(run_id)
        return "other"

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs: Any) -> None:
        tokens_in = tokens_out = 0
        for generations in response.generations:
            for generation in generations:
                meta = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                tokens_in += meta.get("input_tokens", 0)
                tokens_out += meta.get("output_tokens", 0)
        if not (tokens_in or tokens_out):  # some providers report only in llm_output
            usage = (response.llm_output or {}).get("token_usage") or {}
            tokens_in, tokens_out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        bucket = self.totals[self._role_of(run_id)]
        bucket["calls"] += 1
        bucket["input_tokens"] += tokens_in
        bucket["output_tokens"] += tokens_out

    def snapshot(self) -> dict:
        return {role: dict(counts) for role, counts in self.totals.items()}


def next_slot_start(weekday: str, hhmm: str) -> datetime:
    """The next time it is `weekday` at `hhmm` (India time), strictly after now. Today counts if that time is still ahead."""
    now = dates.now_ist()
    hour, minute = (int(part) for part in hhmm.split(":"))
    for ahead in range(8):
        day = now.date() + timedelta(days=ahead)
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=config.TIMEZONE)
        if dates.weekday_name(day) == weekday and start > now:
            return start
    raise ValueError(f"No {weekday} {hhmm} found in the next week.")  # unreachable for a valid weekday


def seed_calendar(message: Message) -> FakeCalendar:
    """A fresh fake calendar. If the message names a demo the parent already booked, that booking is in it.

    The booking is made the way the real tool stores it: for the sender's email address and the child's first name, so
    `reschedule_demo` and `cancel_demo` find it by looking up those two. Nothing else is seeded, and nothing depends on
    chance: the same message gives the same calendar on every run (only "today" moves, so dates move with it).
    """
    calendar = FakeCalendar()
    booked = message.existing_demo
    if booked:
        start = next_slot_start(booked["weekday"], booked["time"])
        first = booked["child_first_name"].strip()
        calendar.create_demo(f"Demo class - {first}", start, start + timedelta(minutes=config.DEMO_CLASS_MINUTES),
                             parseaddr(message.sender)[1], first)
        calendar.calls.clear()  # the seeding is not something the agent did
    return calendar


def trace_from_state(values: dict, *, asked: list[str], seconds: float, usage: dict, finished: bool) -> Trace:
    """Read the finished graph state: the label, every tool call, and what each real tool returned."""
    trace = Trace(label=values.get("classification_decision"), asked=asked, seconds=seconds, usage=usage, finished=finished)
    names: dict[str, str] = {}  # tool_call_id -> tool name
    for message in values.get("messages", []):
        if message.type == "ai":
            for call in message.tool_calls:
                names[call["id"]] = call["name"].lower()
                trace.calls.append(ToolCall(call["name"].lower(), dict(call["args"] or {})))
        elif message.type == "tool":
            name = names.get(message.tool_call_id, "")
            if name and name not in SIGNAL_NAMES:  # the owner's scripted answers are not evidence for any number
                trace.outputs.append((name, str(message.content)))
    return trace


def run_message(
    workflow: StateGraph, message: Message, *, owner: Callable[[dict], dict] = scripted_owner,
    run_id: str = "", max_requests: int = MAX_REQUESTS, real_calendar: bool = False,
) -> Trace:
    """Run one message from start to finish and return what happened. Exceptions (after retries) pass through.

    Default: a fresh seeded fake calendar. `real_calendar=True`: whatever calendar the settings select (the Google test calendar).
    """
    graph = workflow.compile(checkpointer=MemorySaver(), store=InMemoryStore())  # fresh memory for every message
    tracker = UsageTracker()
    config = {
        "configurable": {"thread_id": str(uuid.uuid4())}, "callbacks": [tracker],
        "run_name": f"eval {message.id}", "tags": ["academy-eval", run_id] if run_id else ["academy-eval"],
        "metadata": {"message_id": message.id, "run_id": run_id},  # ids only: never message text
    }
    asked: list[str] = []
    finished = True
    started = time.perf_counter()
    with nullcontext() if real_calendar else use_calendar(seed_calendar(message)):
        result = graph.invoke({"message_input": message.graph_input()}, config)
        while result.get("__interrupt__"):
            if len(asked) >= max_requests:
                finished = False
                break
            request = result["__interrupt__"][0].value[0]
            asked.append(request["action_request"]["action"])
            result = graph.invoke(Command(resume=[owner(request)]), config)
    seconds = time.perf_counter() - started
    return trace_from_state(graph.get_state(config).values, asked=asked, seconds=seconds, usage=tracker.snapshot(), finished=finished)
