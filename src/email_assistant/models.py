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
            # jitter: wait 50-100% of the planned time so parallel callers do not retry in lockstep
            _sleep(wait * random.uniform(0.5, 1.0))
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


def _lazy(role: str, shape: Callable[[Any], Runnable], name: str) -> Runnable:
    """Build a Runnable that creates the real model on first use and retries on rate limits.

    `shape` turns a plain chat model into what the graph needs (tools bound, or
    structured output). It is called once per real model, but on every call for a fake.
    """
    cache: dict[str, Runnable] = {}

    def resolve() -> Runnable:
        if role in _OVERRIDES:  # never cache a fake: the next test brings a new one
            return shape(_OVERRIDES[role])
        if "chain" not in cache:
            cache["chain"] = shape(init_chat_model(model_spec(role), temperature=0.0))
        return cache["chain"]

    def run(value: Any, config: RunnableConfig) -> Any:
        retries, initial, maximum = retry_settings()
        return call_with_backoff(
            lambda: resolve().invoke(value, config),
            max_retries=retries, initial_seconds=initial, max_seconds=maximum,
        )

    return RunnableLambda(run, name=name)


def structured_llm(role: str, schema: type) -> Runnable:
    """A model that must answer with an instance of `schema` (a Pydantic class)."""
    return _lazy(role, lambda model: model.with_structured_output(schema), f"{role}_structured_{schema.__name__}")


def tool_llm(role: str, tools: Sequence[Any], tool_choice: Optional[str] = None) -> Runnable:
    """A model with `tools` available. `tool_choice="required"` or `"any"` forces a tool call."""
    return _lazy(role, lambda model: model.bind_tools(tools, tool_choice=tool_choice), f"{role}_tools")
