"""Read-only lookups in the test Supabase project. Every result carries a `source` field.

These tools never write. They are on the approval allow-list (`guards.READ_ONLY_TOOLS`),
so they run without the owner. Fees, dates and seat counts in a reply must come from here.
"""

import json
from typing import Any

from langchain_core.tools import tool

from email_assistant.academy import dates
from email_assistant.academy.data_client import DataSourceError, get_data_client

SOURCE = "supabase test project"


def _result(table: str, columns: str, **query: Any) -> str:
    try:
        rows = get_data_client().select(table, columns, **query)
    except DataSourceError as exc:
        return json.dumps({"source": f"{SOURCE}: {table}", "error": str(exc)})
    return json.dumps({"source": f"{SOURCE}: {table}", table: rows}, ensure_ascii=False)


@tool
def get_plans() -> str:
    """Get the fee plans: name, classes per month and monthly fee in rupees."""
    return _result("plans", "name,classes_per_month,fee_rupees", filters={"active": "eq.true"}, order="fee_rupees.asc")


@tool
def get_batch_timings() -> str:
    """Get the regular batches: name, days, time range, level and seats left."""
    return _result("batches", "name,days,time_range,level,seats_left", order="name.asc")


@tool
def get_workshops() -> str:
    """Get upcoming workshops: name, date (YYYY-MM-DD), fee in rupees and seats left."""
    return _result("workshops", "name,event_date,fee_rupees,seats_left",
                   filters={"event_date": f"gte.{dates.now_ist().date().isoformat()}"}, order="event_date.asc")


@tool
def get_tournaments() -> str:
    """Get upcoming tournaments: name, date (YYYY-MM-DD), entry fee in rupees and seats left."""
    return _result("tournaments", "name,event_date,entry_fee_rupees,seats_left",
                   filters={"event_date": f"gte.{dates.now_ist().date().isoformat()}"}, order="event_date.asc")


DATA_TOOLS = [get_plans, get_batch_timings, get_workshops, get_tournaments]


def main() -> int:
    """Laptop check (no model, no keys except the test project's anon key): print what each data tool reads.

        python -m email_assistant.academy.data_tools
    """
    from dotenv import load_dotenv

    from email_assistant.academy.settings import AcademyConfigError

    load_dotenv(".env")
    try:
        for data_tool in DATA_TOOLS:
            print(f"{data_tool.name}: {data_tool.invoke({})}")
    except AcademyConfigError as exc:
        print(f"Not ready: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
