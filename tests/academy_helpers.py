"""Shared helpers for the academy graph tests (no API keys, no network)."""

import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from email_assistant.academy.graph import build_graph
from email_assistant.academy.schemas import RouterSchema
from fakes import ScriptedModel

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
