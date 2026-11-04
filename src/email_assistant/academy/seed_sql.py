"""Write the seed rows of `supabase/academy_test_schema.sql` from `config.py`.

The rows in the SQL file are generated, not typed, so the test project always matches the
made-up facts in `config.py` (a test fails if they differ).

    python -m email_assistant.academy.seed_sql            print the seed block
    python -m email_assistant.academy.seed_sql --write    rewrite the block inside the SQL file
"""

import argparse
import re
from pathlib import Path

from email_assistant.academy import config

SQL_FILE = Path("supabase/academy_test_schema.sql")
BEGIN = "-- BEGIN SEED (generated from src/email_assistant/academy/config.py by seed_sql.py - do not edit by hand)"
END = "-- END SEED"

# table -> the made-up rows for it. Each row's keys are the table's column names.
SEED_ROWS = {
    "plans": config.PLANS,
    "batches": config.BATCHES,
    "workshops": config.WORKSHOPS,
    "tournaments": config.TOURNAMENTS,
}


def literal(value: object) -> str:
    """One SQL value: numbers as they are, text in single quotes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_seed() -> str:
    """The generated block, markers included."""
    lines = [BEGIN]
    for table, rows in SEED_ROWS.items():
        columns = list(rows[0])
        values = ",\n".join("  (" + ", ".join(literal(row[c]) for c in columns) + ")" for row in rows)
        lines.append(f"insert into {table} ({', '.join(columns)}) values\n{values};")
    lines.append(END)
    return "\n".join(lines)


def seed_block(sql_text: str) -> str:
    """The seed block found in a SQL file (markers included), or "" if there is none."""
    match = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), sql_text, flags=re.DOTALL)
    return match.group(0) if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Print or write the seed block of the test-project SQL.")
    parser.add_argument("--write", action="store_true", help=f"rewrite the block inside {SQL_FILE}")
    args = parser.parse_args()
    block = render_seed()
    if not args.write:
        print(block)
        return 0
    text = SQL_FILE.read_text(encoding="utf-8")
    old = seed_block(text)
    if not old:
        print(f"No seed block found in {SQL_FILE}.")
        return 1
    SQL_FILE.write_text(text.replace(old, block), encoding="utf-8")
    print(f"Seed block in {SQL_FILE} now matches config.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
