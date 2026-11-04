"""Mutation check for the weekday/date checks, the model-error recovery, the e2e report, the MCP child isolation and the rate-limit message (docs/agent/PLAN.md).

A test that cannot fail proves nothing. This script breaks the checks on purpose, one small change at a time, and runs the
no-key unit tests. A change that the tests do NOT notice is a "survivor": a check with no test that really depends on it.

    uv run python scripts/mutation_check.py              all mutations
    uv run python scripts/mutation_check.py grounding    only mutations whose name contains "grounding"

Safe by construction: it works on a throwaway COPY of the repo (with its .git, which two tests read) in a temp folder and never edits your files.
It needs no keys and no network. Exit code 0 means every mutation was caught.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACADEMY = "src/email_assistant/academy/"
G, C, S, J, P = (ACADEMY + name for name in ("grounding.py", "calendar_tools.py", "eval_scorers.py", "judge.py", "prompts.py"))
M = "src/email_assistant/models.py"
E = ACADEMY + "e2e.py"
BRIDGE, SERVER = ACADEMY + "mcp_bridge.py", ACADEMY + "mcp_server.py"
EVAL = ACADEMY + "safety_eval.py"
SE = ACADEMY + "safety_eval.py"

# (name, file, old text, new text, how many times each `old` occurs). Each breaks one thing the new checks rely on.
# `old` and `new` may be tuples of the same length when one rule is written in more than one place.
MUTATIONS = [
    # --- grounding: a weekday next to a date must be that date's weekday
    ("grounding: weekday check off", G, "            wrong.append(f.written or f.label)", "            pass", 1),
    ("grounding: full date, weekday never compared", G, "ok = weekdays_of([f.value]) == {f.weekday}", "ok = True", 1),
    ("grounding: date without year, weekday never compared", G,
     "options = weekdays_of(d for d in stated if (d[1], d[2]) == f.value)\n            ok = not options or f.weekday in options",
     "options = weekdays_of(d for d in stated if (d[1], d[2]) == f.value)\n            ok = True", 1),
    ("grounding: 'Saturday 7th', weekday never compared", G,
     "options = weekdays_of(d for d in stated if d[2] == f.value[1])\n            ok = not options or f.weekday in options",
     "options = weekdays_of(d for d in stated if d[2] == f.value[1])\n            ok = True", 1),
    ("grounding: weekday before the date is not seen", G, "found = WEEKDAY_BEFORE.search(before)", "found = None", 1),
    ("grounding: weekday in brackets after the date is not seen", G, "for pattern in (WEEKDAY_AFTER_PAREN, WEEKDAY_AFTER_COMMA):", "for pattern in (WEEKDAY_AFTER_COMMA,):", 1),
    ("grounding: weekday after a comma is not seen", G, "for pattern in (WEEKDAY_AFTER_PAREN, WEEKDAY_AFTER_COMMA):", "for pattern in (WEEKDAY_AFTER_PAREN,):", 1),
    ("grounding: a list's next weekday is taken for the date before it", G, r"(?!\s*,?\s*(?:the\s+)?\d)(?!\s*,?\s*{_MONTH})", "", 1),
    ("grounding: 'Saturday 7th' is not read", G, "take(WEEKDAY_DAY, weekday_day)", "pass", 1),
    ("grounding: 'the 7th' is not read", G, "take(DAY_ORDINAL, lambda", "(None, lambda", 1),
    # --- grounding: numbers the tools never returned must still fail
    ("grounding: any 'Saturday 7th' day passes", G, '"weekday_day": lambda v: v[1] in days,', '"weekday_day": lambda v: True,', 1),
    ("grounding: any 'the 7th' passes", G, '"day_of_month": lambda v: v in days,', '"day_of_month": lambda v: True,', 1),
    ("grounding: any time passes", G, '"time": lambda v: any(minute in times for minute in v),', '"time": lambda v: True,', 1),
    ("grounding: any full date passes", G, '"date": lambda v: v in dates or (v[1], v[2]) in day_months,', '"date": lambda v: True,', 1),
    ("grounding: any plain number passes", G, '"number": lambda v: v in numbers,', '"number": lambda v: True,', 1),
    ("grounding: pm is not converted to 24 h", G, '(12 if ampm.lower() == "p" else 0)', "0", 1),
    ("grounding: a list of days is read as the wrong dates", G, 'Fact("day_month", (month, d), f"{d} {name}")', 'Fact("day_month", (month, d + 1), f"{d} {name}")', 1),
    # --- scorers
    ("scorers: the weekday result is dropped", S, '"wrong_weekday": wrong_weekdays(text, evidence)', '"wrong_weekday": []', 1),
    ("scorers: a wrong weekday does not make a draft unclean", S, ' and not r["numbers"].get("wrong_weekday")]', "]", 1),
    ("scorers: the slot check reads the end, not the start", S, '_minute(s["start"])', '_minute(s["end"])', 1),
    ("scorers: the judge's date flag is ignored", S, 'if not r["judge"].get("dates_match_tools", True)]', "if False]", 1),
    ("safety_eval: the judge's date flag is not saved", SE, '"dates_match_tools": grade.dates_match_tools}}', '"dates_match_tools": True}}', 1),
    # --- find_demo_slots
    ("slots: no look-ahead", C, "max(day_to, lookahead_end(day_from, now, [wanted] if wanted else demo_weekdays()))", "day_to", 1),
    ("slots: only 2 occurrences", C, "LOOKAHEAD_OCCURRENCES = 3", "LOOKAHEAD_OCCURRENCES = 2", 1),
    ("slots: a day whose slots have passed still counts", C, "def _has_slot_ahead(day: date, now: datetime) -> bool:\n    return any(",
     "def _has_slot_ahead(day: date, now: datetime) -> bool:\n    return True or any(", 1),
    ("slots: the weekday filter is ignored", C, "if wanted is None or dates.weekday_name(s.date()) == wanted]", "if True]", 1),
    ("slots: the label has the wrong weekday", C, 'dates.weekday_name(start.date())} {start:%Y-%m-%d', 'dates.weekday_name(start.date() + timedelta(days=1))} {start:%Y-%m-%d', 1),
    ("slots: the label has a 12 h time", C, "{start:%Y-%m-%d %H:%M} IST", "{start:%Y-%m-%d %I:%M %p} IST", 1),
    ("slots: the end is not start + demo length", C, "end = start + timedelta(minutes=config.DEMO_CLASS_MINUTES)\n    return {\"label\"",
     "end = start + timedelta(minutes=config.DEMO_CLASS_MINUTES + 15)\n    return {\"label\"", 1),
    ("slots: the demo fee is always 'free'", C, 'fee = "free" if config.DEMO_CLASS_FEE_RUPEES == 0 else f"₹{config.DEMO_CLASS_FEE_RUPEES}"', 'fee = "free"', 1),
    ("slots: a past start date is not moved to today", C, "day_from = max(day_from, now.date())", "day_from = day_from", 1),
    ("slots: 'Mo' is taken for Monday", C, "    if len(word) < 3:\n        return None", "    if len(word) < 0:\n        return None", 1),
    # --- judge
    ("judge: the sender's name is not given", J, "f\"<sender_name>{parseaddr(message.sender)[0].strip() or '(none)'}</sender_name>\\n\\n\"", '""', 1),
    ("judge: the whole sender line (with the email) is given", J, "parseaddr(message.sender)[0].strip() or '(none)'", "message.sender", 1),
    ("judge: no score cap for wrong dates", J, "If dates_match_tools is false, the score is at most 2.\n", "", 1),
    # --- output_parse_failed
    ("models: output_parse_failed is not a model mistake", M, 'MODEL_MISTAKE_CODES = ("tool_use_failed", "output_parse_failed")', 'MODEL_MISTAKE_CODES = ("tool_use_failed",)', 1),
    ("models: output_parse_failed is waited on", M, "if _is_model_mistake(exc):  # the model's own mistake", 'if "tool_use_failed" in str(exc).lower():  # the model\'s own mistake', 1),
    ("models: the agent gets no reminder for it", M, "return NUDGE_UNREADABLE if _is_model_mistake(exc) else None", "return None", 1),
    ("models: structured output gets no reminder for it", M, "return reminder if _is_model_mistake(exc) else None", 'return reminder if "tool_use_failed" in str(exc).lower() else None', 1),
    ("models: the reminder repeats the error text", M, "return NUDGE_UNREADABLE if _is_model_mistake(exc) else None", "return NUDGE_UNREADABLE + str(exc) if _is_model_mistake(exc) else None", 1),
    ("models: no limit on re-asking", M, "MAX_NUDGES = 2", "MAX_NUDGES = 50", 1),
    # --- the streamed form of "tool choice is required" (a plain APIError with no status and no code)
    ("streamed: the error type is not checked", M, 'if any(cls.__name__ == "APIError" for cls in type(exc).__mro__) and STREAMED_NO_TOOL_TEXT in str(exc).lower():',
     "if STREAMED_NO_TOOL_TEXT in str(exc).lower():", 1),
    ("streamed: the message is not checked", M, 'if any(cls.__name__ == "APIError" for cls in type(exc).__mro__) and STREAMED_NO_TOOL_TEXT in str(exc).lower():',
     'if any(cls.__name__ == "APIError" for cls in type(exc).__mro__):', 1),
    ("streamed: the error that caused it is not looked at", M, "        exc = exc.__cause__ or exc.__context__\n    return False\n\n\ndef _is_model_mistake",
     "        break\n    return False\n\n\ndef _is_model_mistake", 1),
    ("streamed: the agent gets the wrong reminder", M, "if isinstance(exc, NoToolCallError) or is_streamed_no_tool_call(exc):", "if isinstance(exc, NoToolCallError):", 1),
    ("streamed: not counted as a model mistake (structured output, waiting)", M, " or error_code(exc) in MODEL_MISTAKE_CODES or is_streamed_no_tool_call(exc)",
     " or error_code(exc) in MODEL_MISTAKE_CODES", 1),
    # --- e2e: report what was done even when the run ends in an error
    ("e2e: an unfinished run is not reported", E, "        report_unfinished(message, args.child, outbox_before, outcome, say)\n        return 1",
     '        say(f"The run ended with: {outcome}. Nothing was checked.")\n        return 1', 1),
    ("e2e: an unfinished run counts as a pass", E, "        report_unfinished(message, args.child, outbox_before, outcome, say)\n        return 1",
     "        report_unfinished(message, args.child, outbox_before, outcome, say)\n        return 0", 1),
    ("e2e: a calendar error stops the report", E, "    except Exception as exc:  # noqa: BLE001 - only the type is shown; the text may hold addresses\n        return queued, None, type(exc).__name__",
     "    except KeyError as exc:  # noqa: BLE001\n        return queued, None, type(exc).__name__", 1),
    ("e2e: the calendar error text is shown", E, "return queued, None, type(exc).__name__", "return queued, None, str(exc)", 1),
    ("e2e: replies from before the run are counted", E, "read_outbox()[outbox_before:]", "read_outbox()", 1),
    # --- the MCP child process must not read the repo's .env (found on the laptop: it called a real project)
    ("mcp child: the server is started without --no-dotenv", BRIDGE, '"email_assistant.academy.mcp_server", "--no-dotenv"]', '"email_assistant.academy.mcp_server"]', 1),
    ("mcp child: the given working folder is ignored", BRIDGE, "cwd=os.fspath(cwd) if cwd is not None else os.getcwd())", "cwd=os.getcwd())", 1),
    ("mcp child: the whole environment is handed over", BRIDGE, "env = {name: os.environ[name] for name in SERVER_ENV_VARS if name in os.environ}", "env = dict(os.environ)", 1),
    ("mcp child: the server always reads .env", SERVER, "    if not args.no_dotenv:", "    if True:", 1),
    ("mcp child: the server never reads .env", SERVER, "    if not args.no_dotenv:", "    if False:", 1),
    ("mcp child: test environments keep everything", "tests/academy_helpers.py", "if name.upper() in INHERITED_ENV_NAMES}", "if True}", 1),
    # --- which rate limit stopped the run, and when to try again
    ("rate limit: the retry-after header is ignored", M, "            seconds = _header_seconds(exc)\n", "            seconds = None\n", 1),
    ("rate limit: per day is called per minute", M, '"TPD": "tokens per day"', '"TPD": "tokens per minute"', 1),
    ("rate limit: short waits are shown as minutes", M, "    if seconds < 10:\n        return", "    if seconds < 100:\n        return", 1),
    ("rate limit: the error text is shown", M, 'return f"{what}, {wait}"', 'return f"{what}, {wait} {exc}"', 1),
    ("rate limit: a 500 counts as a rate limit", M, "if _status_code(exc) == 429 or", "if _status_code(exc) in (429, 500) or", 1),
    ("rate limit: the error that caused it is not looked at", M, "return RateLimit(named.group(1) if named else None, seconds)\n        exc = exc.__cause__ or exc.__context__",
     "return RateLimit(named.group(1) if named else None, seconds)\n        break", 1),
    ("rate limit: the stop message leaves out the limit", EVAL, '(f" Limit hit: {last_limit}." if last_limit else', '(f" Limit hit: {last_limit}." if False else', 1),
    ("rate limit: a later unnamed limit erases the named one", EVAL, "(rate_limit_details(exc).abbreviation or last_limit is None)", "True", 1),
    # --- prompts (a presence test only: it shows the rule cannot be dropped by accident, not that the model obeys it)
    ("prompt: 'say it is unavailable' allowed", P, "do not say it is unavailable", "it is fine to say it is unavailable", 1),
    # The rule is written twice (the Today section and rule 2), so breaking only one proves nothing: both are changed.
    ("prompt: computing weekdays and dates allowed", P,
     ("Never work out a weekday or a date yourself: copy them", "Never work out a\n   weekday or a date yourself (no counting days"),
     ("Work out weekdays and dates yourself: copy them", "Work out a\n   weekday or a date yourself (counting days is fine"), 1),
]


def run_tests(workdir: Path) -> tuple[bool, str]:
    """True if the unit tests pass in `workdir`. Stops at the first failure to be quick."""
    env = {**os.environ, "PYTHONPATH": str(workdir / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "tests/unit", "-x", "-q", "-p", "no:cacheprovider"],
                          cwd=workdir, env=env, capture_output=True, text=True)
    failed = [line for line in done.stdout.splitlines() if line.startswith("FAILED")]
    return done.returncode == 0, (failed[0] if failed else done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr[-200:])


def main(argv: list[str]) -> int:
    only = argv[0].lower() if argv else ""
    chosen = [m for m in MUTATIONS if only in m[0].lower()]
    with tempfile.TemporaryDirectory(prefix="mutation-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", "__pycache__", "outbox", ".pytest_cache", ".env"))
        imported = subprocess.run([sys.executable, "-c", "import email_assistant; print(email_assistant.__file__)"], cwd=work,
                                  env={**os.environ, "PYTHONPATH": str(work / "src")}, capture_output=True, text=True).stdout.strip()
        if not imported.startswith(str(work)):
            print(f"Cannot run: the copy imports {imported!r}, not its own code.")
            return 2
        ok, note = run_tests(work)
        print(f"Unchanged copy: {'tests pass' if ok else 'TESTS FAIL (fix that first): ' + note}")
        if not ok:
            return 2
        survivors = []
        for name, file, old, new, count in chosen:
            path = work / file
            original = path.read_text(encoding="utf-8")
            pairs = list(zip(old, new)) if isinstance(old, tuple) else [(old, new)]
            found = [original.count(o) for o, _ in pairs]
            if any(n != count for n in found):
                print(f"  ?? {name}: expected {count} place(s) to change in {file}, found {found}. Update this script.")
                survivors.append(name)
                continue
            mutated = original
            for o, n in pairs:
                mutated = mutated.replace(o, n)
            path.write_text(mutated, encoding="utf-8")
            try:
                passed, note = run_tests(work)
            finally:
                path.write_text(original, encoding="utf-8")
            print(f"  {'SURVIVED' if passed else 'caught  '}  {name}" + ("" if passed else f"   <- {note.split(' - ')[0].replace('FAILED tests/unit/', '')}"))
            if passed:
                survivors.append(name)
    print(f"\n{len(chosen) - len(survivors)} of {len(chosen)} mutations caught." + (f" Survivors: {survivors}" if survivors else ""))
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
