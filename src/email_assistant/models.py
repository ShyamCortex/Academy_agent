"""Model factory: which AI model each job uses comes from environment variables.

No provider or model name is written in Python. Set these in `.env`:

    TRIAGE_MODEL   "<provider>:<model id>"  decides what a message is (cheap, fast model)
    AGENT_MODEL    "<provider>:<model id>"  drafts replies, calls tools, updates memory
    JUDGE_MODEL    "<provider>:<model id>"  grades answers in evals (use a different
                                            model family from AGENT_MODEL)

Free tiers rate-limit (HTTP 429), so every call retries with exponential backoff:

    MODEL_MAX_RETRIES            default 6
    MODEL_RETRY_INITIAL_SECONDS  default 2    (wait before the 1st retry, doubles each time)
    MODEL_RETRY_MAX_SECONDS      default 60   (the wait never grows beyond this)

Graph files do not build models at import time. They ask for a *lazy* model
(`structured_llm` / `tool_llm`) that is only created on its first call. That is why
importing the code works with no API keys, and why tests can swap in a fake model.
"""

import os
import random
import re
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional, Sequence

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

# role name -> the environment variable that holds its "<provider>:<model id>"
ROLE_ENV_VARS = {
    "triage": "TRIAGE_MODEL",
    "agent": "AGENT_MODEL",
    "judge": "JUDGE_MODEL",
}


class ModelConfigError(ValueError):
    """A model environment variable is missing or malformed."""


def model_spec(role: str) -> str:
    """Return the "<provider>:<model id>" string for a role, or raise a clear error."""
    if role not in ROLE_ENV_VARS:
        raise ModelConfigError(f"Unknown model role {role!r}. Use one of: {', '.join(ROLE_ENV_VARS)}.")
    var = ROLE_ENV_VARS[role]
    value = os.environ.get(var, "").strip()
    if not value:
        raise ModelConfigError(
            f"{var} is not set. Put {var}=<provider>:<model id> in your .env file (see .env.example)."
        )
    if ":" not in value:
        raise ModelConfigError(f"{var}={value!r} must look like '<provider>:<model id>'.")
    return value


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

# HTTP status codes worth retrying: 429 = rate limited, 5xx = the provider had a problem.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# Exception class names for network trouble (different SDKs use different classes).
RETRYABLE_ERROR_NAMES = {
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
}
# Phrases that appear in rate-limit messages when the status code is hidden inside text.
RETRYABLE_TEXT = (
    "resource_exhausted", "rate limit", "rate_limit", "too many requests", "429 ", "503 ", "unavailable", "overloaded",
)


def _status_code(exc: BaseException) -> Optional[int]:
    """Find an HTTP status code on an exception, whichever way its SDK stores it."""
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def is_retryable(exc: BaseException) -> bool:
    """True if waiting and trying again could help (rate limit, server error, network)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:  # also look at the error that caused this one
        seen.add(id(exc))
        if "tool_use_failed" in str(exc).lower():  # the model's own mistake: waiting will not fix it
            return False
        if _status_code(exc) in RETRYABLE_STATUS_CODES:
            return True
        if type(exc).__name__ in RETRYABLE_ERROR_NAMES:
            return True
        if any(marker in str(exc).lower() for marker in RETRYABLE_TEXT):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _sleep(seconds: float) -> None:
    """Wait. Tests replace this so they do not really wait."""
    time.sleep(seconds)


def call_with_backoff(
    fn: Callable[[], Any], *, max_retries: int, initial_seconds: float, max_seconds: float
) -> Any:
    """Call fn(); on a retryable error wait (2s, 4s, 8s... with jitter) and try again."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable(exc):
                raise
            wait = min(max_seconds, initial_seconds * 2**attempt)
            # jitter: wait 80-100% of the planned time so parallel callers do not retry in lockstep
            _sleep(wait * random.uniform(0.8, 1.0))
            attempt += 1


def _env_number(name: str, default: float, cast: Callable[[str], Any]) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ModelConfigError(f"{name}={raw!r} is not a valid number.") from None


def retry_settings() -> tuple[int, float, float]:
    """Read the retry settings from the environment (with defaults)."""
    return (
        _env_number("MODEL_MAX_RETRIES", 6, int),
        _env_number("MODEL_RETRY_INITIAL_SECONDS", 2.0, float),
        _env_number("MODEL_RETRY_MAX_SECONDS", 60.0, float),
    )


# ---------------------------------------------------------------------------
# Tool-calling mistakes (seen with gpt-oss on Groq)
# ---------------------------------------------------------------------------

# How many times we ask the model to fix a tool-calling mistake before giving up.
MAX_NUDGES = 2

NUDGE_NO_TOOL = (
    "You must reply by calling one of the available tools, not with plain text. "
    "If the task is complete, call the Done tool."
)
NUDGE_BAD_ARGS = (
    "Your last tool call did not match the tool's argument schema. Call the tool again with valid "
    "arguments. Dates and times are plain strings in the format the tool describes."
)


class NoToolCallError(RuntimeError):
    """The model had to call a tool, but answered with plain text."""


def tool_call_nudge(exc: BaseException) -> Optional[str]:
    """If `exc` is a tool-calling mistake the model can fix, return a short reminder for it.

    Two mistakes are known:
    - "Tool choice is required, but model did not call a tool": a forced tool call, but the
      model wrote a closing sentence instead (usually because the work was already done).
    - "tool call validation failed ... did not match schema": the model sent an argument in
      the wrong shape. Groq checks arguments strictly, e.g. a `datetime` needs seconds and a
      time zone.
    The reminder never repeats the model's output or the tool arguments (they may hold personal data).
    """
    if isinstance(exc, NoToolCallError):
        return NUDGE_NO_TOOL
    text = str(exc).lower()
    if "tool_use_failed" not in text:
        return None
    if "did not call a tool" in text:
        return NUDGE_NO_TOOL
    if "validation failed" in text or "did not match schema" in text:
        return NUDGE_BAD_ARGS
    return None


# Structured output (the judge, triage, memory). With Groq, `with_structured_output` is a forced call
# of one tool named after the schema, so the same mistakes happen: a 400 `tool_use_failed` (no call, or
# arguments that break the schema), no tool call at all (the answer comes back as None), or values our
# own checks refuse (a pydantic ValidationError).
NUDGE_STRUCTURED = (
    "Answer only by calling the {name} tool once, with every field filled in and each value of the type "
    "its schema gives (numbers as numbers, true/false as booleans). Do not answer in plain text."
)


class NoStructuredAnswerError(RuntimeError):
    """The model had to answer in a fixed format (a schema), but gave nothing usable."""


def structured_answer_nudge(schema_name: str) -> Callable[[BaseException], Optional[str]]:
    """The `recover` function for structured output: a reminder for mistakes the model can fix, else None.

    Like `tool_call_nudge`, the reminder never repeats the model's output or the error text.
    """
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    reminder = NUDGE_STRUCTURED.format(name=schema_name)

    def nudge(exc: BaseException) -> Optional[str]:
        if isinstance(exc, (NoStructuredAnswerError, ValidationError, OutputParserException)):
            return reminder
        return reminder if "tool_use_failed" in str(exc).lower() else None

    return nudge


# ---------------------------------------------------------------------------
# Describing an error without its text
# ---------------------------------------------------------------------------

# A provider error code is a short identifier such as "tool_use_failed" or "rate_limit_exceeded".
_CODE_SHAPE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_CODE_IN_TEXT = re.compile(r"""['"]code['"]\s*:\s*['"]([A-Za-z0-9_.\-]{1,64})['"]""")


def error_code(exc: BaseException) -> Optional[str]:
    """The provider's error code (Groq: `body["error"]["code"]`, e.g. "tool_use_failed"), or None.

    Only a short identifier is ever returned, never message text: the text of a model error can quote
    the parent's message or the model's output. Looks at the error and the errors that caused it.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            inner = body.get("error", body)
            code = inner.get("code") if isinstance(inner, dict) else None
            if isinstance(code, str) and _CODE_SHAPE.fullmatch(code):
                return code
        code = getattr(exc, "code", None)
        if isinstance(code, str) and _CODE_SHAPE.fullmatch(code):
            return code
        found = _CODE_IN_TEXT.search(str(exc))  # SDKs that only keep the body as text: take the code, nothing else
        if found:
            return found.group(1)
        exc = exc.__cause__ or exc.__context__
    return None


def describe_error(exc: BaseException) -> str:
    """Safe to print or log: the error's type and its provider code, never its message text."""
    code = error_code(exc)
    return f"{type(exc).__name__}, code {code}" if code else type(exc).__name__


# ---------------------------------------------------------------------------
# Lazy models
# ---------------------------------------------------------------------------

# Tests put fake models here, keyed by role. A fake needs the same two methods a real
# chat model has: `bind_tools(...)` and `with_structured_output(...)`.
_OVERRIDES: dict[str, Any] = {}


@contextmanager
def use_fake_models(**fakes: Any):
    """Temporarily replace models, e.g. `with use_fake_models(triage=fake, agent=fake2): ...`."""
    unknown = set(fakes) - set(ROLE_ENV_VARS)
    if unknown:
        raise ModelConfigError(f"Unknown model role(s): {sorted(unknown)}")
    previous = dict(_OVERRIDES)
    _OVERRIDES.update(fakes)
    try:
        yield
    finally:
        _OVERRIDES.clear()
        _OVERRIDES.update(previous)


def _lazy(
    role: str,
    shape: Callable[[Any], Runnable],
    name: str,
    *,
    check: Optional[Callable[[Any], None]] = None,
    recover: Optional[Callable[[BaseException], Optional[str]]] = None,
) -> Runnable:
    """Build a Runnable that creates the real model on first use and retries on rate limits.

    `shape` turns a plain chat model into what the graph needs (tools bound, or
    structured output). It is called once per real model, but on every call for a fake.
    `check(result)` may raise if the answer is unusable. `recover(error)` may return a reminder
    to append to the conversation; the call is then repeated (at most MAX_NUDGES times).
    """
    cache: dict[str, Runnable] = {}

    def resolve() -> Runnable:
        if role in _OVERRIDES:  # never cache a fake: the next test brings a new one
            return shape(_OVERRIDES[role])
        if "chain" not in cache:
            cache["chain"] = shape(init_chat_model(model_spec(role), temperature=0.0))
        return cache["chain"]

    def attempt(value: Any, config: RunnableConfig) -> Any:
        retries, initial, maximum = retry_settings()

        def once() -> Any:
            result = resolve().invoke(value, config)
            if check is not None:
                check(result)
            return result

        return call_with_backoff(once, max_retries=retries, initial_seconds=initial, max_seconds=maximum)

    def run(value: Any, config: RunnableConfig) -> Any:
        nudges = 0
        while True:
            try:
                return attempt(value, config)
            except Exception as exc:
                reminder = recover(exc) if recover else None
                if reminder is None or nudges >= MAX_NUDGES:
                    raise
                value = list(value) + [{"role": "user", "content": reminder}]
                nudges += 1

    return RunnableLambda(run, name=name)


def structured_llm(role: str, schema: type) -> Runnable:
    """A model that must answer with an instance of `schema` (a Pydantic class).

    If the answer is missing or does not fit the schema (including a Groq 400 `tool_use_failed`), the
    model is reminded and asked again, up to MAX_NUDGES times, the same way `tool_llm` does it.
    """
    def check(result: Any) -> None:
        if result is None:
            raise NoStructuredAnswerError(f"The model gave no {schema.__name__} answer.")

    return _lazy(
        role, lambda model: model.with_structured_output(schema), f"{role}_structured_{schema.__name__}",
        check=check, recover=structured_answer_nudge(schema.__name__),
    )


def tool_llm(role: str, tools: Sequence[Any], tool_choice: Optional[str] = None) -> Runnable:
    """A model with `tools` available. `tool_choice="required"` or `"any"` forces a tool call.

    When a tool call is forced and the model still answers in plain text (or sends arguments
    that break the schema), the model is reminded and asked again, up to MAX_NUDGES times.
    """
    forced = tool_choice in ("required", "any")

    def check(result: Any) -> None:
        if forced and not getattr(result, "tool_calls", None):
            raise NoToolCallError("The model answered without calling a tool.")

    return _lazy(
        role, lambda model: model.bind_tools(tools, tool_choice=tool_choice), f"{role}_tools",
        check=check, recover=tool_call_nudge,
    )


# ---------------------------------------------------------------------------
# Eval guard
# ---------------------------------------------------------------------------

def require_distinct_judge() -> None:
    """Refuse to run an eval when the judge is the same model as the drafter.

    A model grading its own answers is biased towards them. Call this at the start of every
    eval entry point. If either variable is unset, nothing is checked here: `get_model`-style
    errors will name the missing variable when the model is first used.
    """
    judge = os.environ.get("JUDGE_MODEL", "").strip()
    agent = os.environ.get("AGENT_MODEL", "").strip()
    if judge and agent and judge.lower() == agent.lower():
        raise ModelConfigError(
            f"JUDGE_MODEL and AGENT_MODEL are both {agent!r}. An eval must not use the same model "
            "to write and to grade the answers. Set JUDGE_MODEL in .env to a model from a "
            "different family than AGENT_MODEL (see .env.example)."
        )
