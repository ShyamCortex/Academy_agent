"""When a free-tier run stops on rate limits, say WHICH limit (per minute or per day) and when to try again.

Only the four short limit names (RPM, RPD, TPM, TPD) and a number of seconds are taken from the provider's error. Its text
can quote content, so it is never printed (hard rule 5).
"""

import groq
import httpx
import pytest

from email_assistant import models
from email_assistant.models import RateLimit, describe_rate_limit, format_wait, rate_limit_details

PRIVATE = "ZQX-PRIVATE-TEXT"


def groq_429(text="", headers=None, status=429, code="rate_limit_exceeded"):
    """A real Groq SDK rate-limit error, built the way the SDK builds it from the HTTP answer."""
    request = httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions")
    message = f"Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` {text} {PRIVATE}"
    body = {"error": {"message": message, "type": "tokens", "code": code}}
    cls = groq.RateLimitError if status == 429 else groq.InternalServerError
    return cls(f"Error code: {status} - {body}", response=httpx.Response(status, request=request, headers=headers or {}), body=body)


# ---- the limit type -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("abbreviation,name", [("TPD", "tokens per day"), ("TPM", "tokens per minute"), ("RPD", "requests per day"), ("RPM", "requests per minute")])
def test_each_limit_is_named_in_plain_words(abbreviation, name):
    error = groq_429(f"on {name} ({abbreviation}): Limit 100000, Used 99990, Requested 50. Please try again in 5s.")
    assert describe_rate_limit(error) == f"{name} ({abbreviation}), retry after about 5.0s"
    assert rate_limit_details(error).window == name.split(" ", 1)[1]


def test_per_minute_and_per_day_are_told_apart_by_window():
    assert rate_limit_details(groq_429("on tokens per minute (TPM): Limit 8000")).window == "per minute"
    assert rate_limit_details(groq_429("on requests per day (RPD): Limit 1000")).window == "per day"
    assert RateLimit(None, 3.0).window is None


def test_a_limit_the_error_does_not_name_is_said_to_be_unnamed_not_guessed():
    assert describe_rate_limit(groq_429("Please try again in 7s.")) == "limit type not named by the provider, retry after about 7.0s"


# ---- when to try again ----------------------------------------------------------------------------------------------

def test_the_retry_after_header_is_used_first():
    error = groq_429("on tokens per day (TPD): Please try again in 3s.", headers={"retry-after": "863"})
    assert describe_rate_limit(error) == "tokens per day (TPD), retry after about 14m 23s"


@pytest.mark.parametrize("wait,shown", [("7m12s", "7m 12s"), ("1h2m3s", "1h 2m 3s"), ("2.5s", "2.5s"), ("800ms", "0.8s"), ("1h", "1h"), ("14m0s", "14m")])
def test_the_try_again_time_in_the_text_is_read_when_there_is_no_header(wait, shown):
    assert describe_rate_limit(groq_429(f"on requests per day (RPD): Limit 1000. Please try again in {wait}.")) == f"requests per day (RPD), retry after about {shown}"


def test_no_retry_time_anywhere_says_so():
    assert describe_rate_limit(groq_429("on tokens per minute (TPM): Limit 8000.")) == "tokens per minute (TPM), no retry time given"


@pytest.mark.parametrize("bad", ["soon", "", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_header_that_is_not_a_number_of_seconds_is_ignored(bad):
    error = groq_429("on tokens per day (TPD): Please try again in 9m.", headers={"retry-after": bad})
    assert describe_rate_limit(error) == "tokens per day (TPD), retry after about 9m"


@pytest.mark.parametrize("seconds,shown", [(0.8, "0.8s"), (3.24, "3.2s"), (10, "10s"), (59.6, "1m"), (60, "1m"), (862, "14m 22s"), (3725, "1h 2m 5s"), (7200, "2h")])
def test_waits_are_shown_in_short_form(seconds, shown):
    assert format_wait(seconds) == shown


# ---- only rate limits, and never the error's text -------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    groq_429("on tokens per day (TPD)", status=500, code="internal"),      # a server error that quotes a limit name
    RuntimeError("TPD limit"),
    ValueError("429 rate limit TPM"),                                      # words only: no status, no code
])
def test_other_errors_give_nothing(error):
    assert rate_limit_details(error) is None and describe_rate_limit(error) is None


def test_the_error_that_caused_it_is_looked_at_too():
    try:
        try:
            raise groq_429("on requests per day (RPD): Please try again in 4m.")
        except groq.RateLimitError as inner:
            raise RuntimeError("call failed") from inner
    except RuntimeError as outer:
        assert describe_rate_limit(outer) == "requests per day (RPD), retry after about 4m"


def test_nothing_from_the_error_text_is_returned():
    error = groq_429("on tokens per day (TPD): Limit 100000, Used 99990, Requested 50. Please try again in 5m.", headers={"retry-after": "300"})
    shown = describe_rate_limit(error)
    assert PRIVATE not in shown and "org_x" not in shown and "gpt-oss" not in shown and "100000" not in shown
    assert set(shown) <= set("abcdefghijklmnopqrstuvwxyz0123456789 (),.TPDRMS")


def test_the_recovery_is_untouched_by_reading_the_details():
    assert models.is_retryable(groq_429("on tokens per day (TPD)"))
