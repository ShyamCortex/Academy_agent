"""Shared helpers for the academy graph tests (no API keys, no network)."""

import base64
import json
import uuid
from datetime import datetime

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from email_assistant.academy import config
from email_assistant.academy.graph import build_graph
from email_assistant.academy.schemas import RouterSchema
from fakes import ScriptedModel

# The clock every unit test sees (a Wednesday, 10:00 India time). Slots before this are in the past.
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=config.TIMEZONE)
TEST_URL = "https://test-project.supabase.co"


def make_jwt(role: str) -> str:
    """A fake Supabase-style key: three dot-separated parts, the middle one says the role. Not signed."""
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'HS256'})}.{part({'role': role})}.fake-signature"


ANON_KEY = make_jwt("anon")


class SeedServer:
    """A pretend PostgREST server that answers from the rows in `config.py`. Records every request."""

    def __init__(self):
        self.tables = {
            "plans": [dict(r, active=True) for r in config.PLANS],
            "batches": [dict(r) for r in config.BATCHES],
            "workshops": [dict(r) for r in config.WORKSHOPS],
            "tournaments": [dict(r) for r in config.TOURNAMENTS],
        }
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        if table not in self.tables:
            return httpx.Response(404, json={"message": "no such table"})
        rows = self.tables[table]
        params = request.url.params
        if "event_date" in params and params["event_date"].startswith("gte."):
            rows = [r for r in rows if r["event_date"] >= params["event_date"][4:]]
        if params.get("active") == "eq.true":
            rows = [r for r in rows if r.get("active")]
        columns = params["select"].split(",")
        return httpx.Response(200, json=[{c: r[c] for c in columns} for r in rows])


MESSAGE = {"from": "Parent A <parent.a@example.invalid>", "text": "Hello, can my son try a demo class this week?"}


def route(label: str) -> RouterSchema:
    return RouterSchema(reasoning="test", classification=label)


def triage_says(label: str) -> ScriptedModel:
    return ScriptedModel(route(label))


class Run:
    """One graph run with its own checkpointer and store, so a test can pause and resume."""

    def __init__(self, graph=None, store=None):
        self.store = store or InMemoryStore()
        self.graph = (graph or build_graph()).compile(checkpointer=MemorySaver(), store=self.store)
        self.config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    def start(self, message=None):
        return self.graph.invoke({"message_input": message or MESSAGE}, self.config)

    def answer(self, **response):
        """Answer the owner's pending question, e.g. answer(type="accept")."""
        return self.graph.invoke(Command(resume=[response]), self.config)

    @property
    def state(self):
        return self.graph.get_state(self.config)

    def drafter_messages(self):
        """Messages inside the paused drafter (the parent's saved state shows them only after it finishes)."""
        snapshot = self.graph.get_state(self.config, subgraphs=True)
        return snapshot.tasks[0].state.values["messages"]

    @staticmethod
    def pending(result):
        """The request shown to the owner, or None if the run did not stop."""
        interrupts = result.get("__interrupt__")
        return interrupts[0].value[0] if interrupts else None
