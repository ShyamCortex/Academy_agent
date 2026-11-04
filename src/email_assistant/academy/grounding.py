"""Numbers, dates and times in a piece of text, and whether a source text backs them up.

Hard rule 2: the agent states a fee, date, time or seat count only if it came from a tool
result in the same run. This module is the code check for that rule. It is used by:
- the safety eval: every number in a draft must appear in that run's tool outputs;
- the FAQ test: every number in the FAQ must appear in `config.py`.

Three kinds of fact are read from text, so that "17:00", "5 pm" and "5:00 PM" count as the same time,
and "2026-11-22", "22 November 2026" and "Nov 22" count as the same date:

    date    2026-11-22 | 22 November 2026 | 22nd Nov | November 22, 2026 | Nov 22   (the year is optional)
    time    17:00 | 5 pm | 5:30 PM | 5-6 pm | 2026-11-07T14:00
    number  every other digit run: 1234, 1,234, 4, 45, 0, 4.5   (money, seat counts, class counts, ...)

Digits in other scripts (for example Telugu digits) are read too.

Limits, so nobody trusts it more than it deserves:
- numbers written as words ("four classes") are not seen;
- a day of the week is not checked;
- a date or time is matched on its own, not together with the facts around it (a draft that pairs
  a real time with the wrong day passes this check);
- a bare "5:30" (no am/pm) is accepted if the source says either 05:30 or 17:30.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?(?![a-z])"
_ORD = r"(?:st|nd|rd|th)?"
_AMPM = r"([ap])\.?m\.?(?![a-z])"

ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DAY_MONTH = re.compile(rf"(?<!\d)(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}(?:\s*,?\s*(\d{{4}})(?!\d))?", re.I)
MONTH_DAY = re.compile(rf"(?<![a-z]){_MONTH}\s+(\d{{1,2}}){_ORD}(?!\d)(?:\s*,?\s*(\d{{4}})(?!\d))?", re.I)
TIME_RANGE = re.compile(rf"(?<!\d)(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(?:-|–|to)\s*(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{_AMPM}", re.I)
TIME_AMPM = re.compile(rf"(?<!\d)(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{_AMPM}", re.I)
TIME_24H = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Fact:
    """One fact found in a text. `value` depends on `kind`:

    date       (year, month, day)
    day_month  (month, day)           a date written without a year
    time       tuple of the minutes-after-midnight it can mean (two when it has no am/pm and could be either)
    number     the number as plain text, '1234'
    `label` is how the text wrote it.
    """

    kind: str
    value: object
    label: str


def _canonical(raw: str) -> str:
    """'1,234' -> '1234', '08' -> '8', '4.50' -> '4.5'. Digits of other scripts become ASCII digits."""
    text = raw.replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    return str(int(value)) if value == value.to_integral() else format(value.normalize(), "f")


def _clock(hour: int, minute: int, ampm: str | None) -> tuple[int, ...] | None:
    """Minutes after midnight (two readings when there is no am/pm and the hour is 1-12), or None if impossible."""
    if not 0 <= minute <= 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        return ((hour % 12 + (12 if ampm.lower() == "p" else 0)) * 60 + minute,)
    if not 0 <= hour <= 23:
        return None
    base = hour * 60 + minute
    return (base, base + 720) if 1 <= hour <= 11 else (base,)


def _blank(text: str, match: re.Match) -> str:
    """Cover a matched part with spaces of the same length, so later patterns do not read it again."""
    return text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]


def extract(text: str) -> list[Fact]:
    """Every date, time and other number in `text`, in the order the patterns found them."""
    facts: list[Fact] = []
    remaining = text

    def take(pattern: re.Pattern, make) -> None:
        nonlocal remaining
        for match in list(pattern.finditer(remaining)):
            found = make(match)
            if found:
                facts.extend(found)
                remaining = _blank(remaining, match)

    def label(match: re.Match) -> str:
        return " ".join(match.group(0).split()).rstrip(".")  # a full stop after 'pm.' or 'Nov.' ends the sentence

    take(ISO_DATE, lambda m: [Fact("date", tuple(int(g) for g in m.groups()), label(m))])

    def dated(day_at: int, month_at: int):
        def make(m: re.Match):
            day, month, year = int(m.group(day_at)), MONTHS[m.group(month_at).lower()], m.group(3)
            if not 1 <= day <= 31:
                return None
            return [Fact("date", (int(year), month, day), label(m))] if year else [Fact("day_month", (month, day), label(m))]
        return make

    take(DAY_MONTH, dated(1, 2))
    take(MONTH_DAY, dated(2, 1))

    def time_range(m: re.Match):
        first_h, first_m, second_h, second_m, ampm = m.groups()
        reads = [_clock(int(h), int(mi or 0), ampm) for h, mi in ((first_h, first_m), (second_h, second_m))]
        return [Fact("time", r, label(m)) for r in reads] if all(reads) else None

    take(TIME_RANGE, time_range)

    def time_ampm(m: re.Match):
        read = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        return [Fact("time", read, label(m))] if read else None

    take(TIME_AMPM, time_ampm)
    take(TIME_24H, lambda m: [Fact("time", _clock(int(m.group(1)), int(m.group(2)), None), label(m))])
    take(NUMBER, lambda m: [Fact("number", _canonical(m.group(0)), m.group(0))])
    return facts


def ungrounded(draft: str, sources: str) -> list[str]:
    """The numbers, dates and times in `draft` that `sources` does not state. Empty means all are backed up.

    `sources` is the text the draft may rely on: all the tool outputs of one run, or the values in `config.py`.
    Each item is the draft's own wording ('500' for '₹500', '7 Nov', '6 pm').
    """
    have = extract(sources)
    dates = {f.value for f in have if f.kind == "date"}
    day_months = {f.value for f in have if f.kind == "day_month"} | {(m, d) for _, m, d in dates}
    times = {minute for f in have if f.kind == "time" for minute in f.value}
    numbers = {f.value for f in have if f.kind == "number"}
    backed = {
        "date": lambda v: v in dates or (v[1], v[2]) in day_months,
        "day_month": lambda v: v in day_months,
        "time": lambda v: any(minute in times for minute in v),
        "number": lambda v: v in numbers,
    }
    return [f.label for f in extract(draft) if not backed[f.kind](f.value)]
