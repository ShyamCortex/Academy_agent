"""Model factory: env-var config, retry with backoff, lazy creation. No API keys needed."""

import pytest

from email_assistant import models
from email_assistant.models import (
    ModelConfigError,
    call_with_backoff,
    is_retryable,
    model_spec,
    retry_settings,
    structured_llm,
    use_fake_models,
)
from email_assistant.schemas import RouterSchema
from fakes import ScriptedModel


class RateLimited(Exception):
    """Looks like an SDK rate-limit error: it carries an HTTP status code."""

    def __init__(self, status_code=429):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.fixture
def sleeps(monkeypatch):
    """Replace the real wait with a list, so tests do not really wait."""
    recorded = []
    monkeypatch.setattr(models, "_sleep", recorded.append)
    return recorded


# --- config -----------------------------------------------------------------

@pytest.mark.parametrize("role,var", [("triage", "TRIAGE_MODEL"), ("agent", "AGENT_MODEL"), ("judge", "JUDGE_MODEL")])
def test_missing_variable_is_named_in_the_error(monkeypatch, role, var):
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(ModelConfigError, match=var):
        model_spec(role)


def test_value_without_provider_is_rejected_and_named(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "just-a-model-id")
    with pytest.raises(ModelConfigError, match="AGENT_MODEL"):
        model_spec("agent")


def test_valid_value_is_returned(monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "someprovider:some-model")
    assert model_spec("judge") == "someprovider:some-model"


def test_unknown_role_is_rejected():
    with pytest.raises(ModelConfigError, match="Unknown model role"):
        model_spec("poet")


def test_bad_retry_number_names_the_variable(monkeypatch):
    monkeypatch.setenv("MODEL_MAX_RETRIES", "lots")
    with pytest.raises(ModelConfigError, match="MODEL_MAX_RETRIES"):
        retry_settings()


def test_retry_defaults(monkeypatch):
    for var in ("MODEL_MAX_RETRIES", "MODEL_RETRY_INITIAL_SECONDS", "MODEL_RETRY_MAX_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    assert retry_settings() == (6, 2.0, 60.0)


# --- which errors are worth retrying -----------------------------------------

def test_status_429_and_5xx_are_retryable():
    assert is_retryable(RateLimited(429))
    assert is_retryable(RateLimited(503))


def test_rate_limit_text_is_retryable():
    assert is_retryable(RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"))


def test_bad_request_is_not_retryable():
    assert not is_retryable(RateLimited(400))
    assert not is_retryable(ValueError("schema is wrong"))


def test_cause_chain_is_followed():
    try:
        try:
            raise RateLimited(429)
        except RateLimited as inner:
            raise RuntimeError("wrapped by the SDK") from inner
    except RuntimeError as outer:
        assert is_retryable(outer)


# --- backoff -----------------------------------------------------------------

def _flaky(failures, error=RateLimited):
    """A function that fails `failures` times, then returns "ok". Counts its calls."""
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise error()
        return "ok"

    return fn, state


def test_waits_double_each_retry_with_jitter(sleeps):
    fn, state = _flaky(3)
    assert call_with_backoff(fn, max_retries=6, initial_seconds=2, max_seconds=60) == "ok"
    assert state["calls"] == 4
    assert len(sleeps) == 3
    for wait, planned in zip(sleeps, [2, 4, 8]):
        assert 0.5 * planned <= wait <= planned  # jitter: 50-100% of the planned wait


def test_wait_never_exceeds_the_cap(sleeps):
    fn, _ = _flaky(4)
    call_with_backoff(fn, max_retries=6, initial_seconds=2, max_seconds=5)
    assert max(sleeps) <= 5


def test_non_retryable_error_is_raised_at_once(sleeps):
    fn, state = _flaky(1, error=ValueError)
    with pytest.raises(ValueError):
        call_with_backoff(fn, max_retries=6, initial_seconds=2, max_seconds=60)
    assert state["calls"] == 1 and sleeps == []


def test_gives_up_after_max_retries(sleeps):
    fn, state = _flaky(99)
    with pytest.raises(RateLimited):
        call_with_backoff(fn, max_retries=2, initial_seconds=1, max_seconds=60)
    assert state["calls"] == 3 and len(sleeps) == 2  # 1 try + 2 retries


# --- lazy models and the fake hook -------------------------------------------

def test_creating_a_model_needs_no_env_vars(monkeypatch):
    for var in ("TRIAGE_MODEL", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    llm = structured_llm("triage", RouterSchema)  # must not raise: nothing is built yet
    with pytest.raises(ModelConfigError, match="TRIAGE_MODEL"):
        llm.invoke("hello")  # the error appears only when the model is first used


def test_fake_model_is_used_and_rate_limit_is_retried(sleeps):
    answer = RouterSchema(reasoning="because", classification="respond")
    fake = ScriptedModel(RateLimited(429), answer)
    with use_fake_models(triage=fake):
        result = structured_llm("triage", RouterSchema).invoke("hello")
    assert result == answer
    assert fake.schema is RouterSchema
    assert len(sleeps) == 1  # waited once, then the second try worked


def test_fakes_are_removed_afterwards():
    with use_fake_models(agent=ScriptedModel()):
        assert "agent" in models._OVERRIDES
    assert models._OVERRIDES == {}


def test_fake_for_unknown_role_is_rejected():
    with pytest.raises(ModelConfigError):
        with use_fake_models(poet=ScriptedModel()):
            pass
