"""Fake chat model for tests that run with no API keys.

A real chat model has two methods the graphs use: `bind_tools(...)` and
`with_structured_output(...)`. `ScriptedModel` has the same two methods, but it
returns pre-written answers one at a time instead of calling an AI service.

    fake = ScriptedModel(RouterSchema(reasoning="r", classification="respond"))
    with use_fake_models(triage=fake):
        ...  # the graph's triage step now gets the scripted answer
"""

import itertools

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

_call_ids = itertools.count(1)


class ScriptedModel:
    """Returns the next scripted answer each call. A scripted exception is raised instead."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []  # every input the model received, so tests can look at it
        self.tool_choice = None  # what the graph asked for in bind_tools
        self.bound_tools = None
        self.schema = None  # what the graph asked for in with_structured_output

    def _next(self, messages):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("ScriptedModel ran out of scripted answers")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def bind_tools(self, tools, tool_choice=None, **kwargs):
        self.bound_tools = tools
        self.tool_choice = tool_choice
        return RunnableLambda(self._next)

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        return RunnableLambda(self._next)


def tool_call_message(name: str, **args) -> AIMessage:
    """An AI message that asks to call one tool, like a real model does."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_call_ids)}"}])


class HashingEmbedder:
    """A fake embedder for tests: a bag of words, hashed into 384 slots. No model, no download, same result every time.

    It matches by shared words only (not by meaning), so it proves the plumbing, not the quality of a real model.
    """

    model_name = "hashing-fake"
    STOP = frozenset("a an the is are do does i we you my your of to for in on at it be can how what when which who".split())

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        import math
        import re
        import zlib

        vector = [0.0] * self.dim
        for word in re.findall(r"[^\W_]+", text.lower()):
            if word not in self.STOP:
                vector[zlib.crc32(word.encode()) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def embed_query(self, text):
        self.queries.append(text)
        return self._vector(text)

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]
