"""Date and time helpers. Everything is India time (`Asia/Kolkata`).

Tools take dates and times as plain strings, never `datetime`: Groq checks a `datetime`
argument as a strict RFC 3339 date-time and rejects a model's "2026-11-07T14:00" (no seconds,
no time zone). Strings are parsed here instead.
"""

from datetime import date, datetime

from email_assistant.academy import config

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def now_ist() -> datetime:
    """The current moment in India. Read on every call; tests replace this function."""
    return datetime.now(config.TIMEZONE)


def weekday_name(day: date) -> str:
    """English weekday name. Not `strftime("%A")`, which follows the computer's language."""
    return WEEKDAYS[day.weekday()]


def parse_local_datetime(value: str) -> datetime:
    """Read "2026-11-07T14:00", "2026-11-07 14:00" (seconds optional) as India time.

    A value that carries its own offset (e.g. "+05:30" or "Z") is converted to India time.
    Raises ValueError if the text is not a date and time.
    """
    text = value.strip().replace(" ", "T", 1)
    if "T" not in text:
        raise ValueError("a time is needed, like 2026-11-07T14:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=config.TIMEZONE)
    return parsed.astimezone(config.TIMEZONE)


def parse_day(value: str) -> date:
    """Read "2026-11-07" (anything after the first 10 characters is ignored)."""
    return date.fromisoformat(value.strip()[:10])


def iso_minutes(moment: datetime) -> str:
    """"2026-11-07T14:00" in India time: the form the tools show to the model."""
    return moment.astimezone(config.TIMEZONE).strftime("%Y-%m-%dT%H:%M")
