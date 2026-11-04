"""The academy tool set, in one place.

    read-only (no approval)   find_demo_slots, get_plans, get_batch_timings, get_workshops, get_tournaments
    write (owner approves)    create_demo_event, reschedule_demo, cancel_demo, send_reply
    signals                   Question (ask the owner), Done (finish)

Which tools skip approval is NOT decided here. `guards.READ_ONLY_TOOLS` is the only list, and
it is deny-by-default: a tool added to this file without being added there still asks the owner.

With USE_MCP=1 the read-only tools are called through the MCP server (`mcp_server.py`) instead of
in-process. They keep the same names, descriptions and arguments, so the graph and the tests do not
change. Write tools are never exposed over MCP: an MCP client would skip the owner's approval.
"""

from langchain_core.tools import BaseTool

from email_assistant.academy import settings
from email_assistant.academy.calendar_tools import CALENDAR_READ_TOOLS, CALENDAR_WRITE_TOOLS
from email_assistant.academy.data_tools import DATA_TOOLS
from email_assistant.academy.outbox import send_reply
from email_assistant.tools.default.email_tools import Done, Question

READ_ONLY_TOOL_OBJECTS: list[BaseTool] = [*CALENDAR_READ_TOOLS, *DATA_TOOLS]
WRITE_TOOLS: list[BaseTool] = [*CALENDAR_WRITE_TOOLS, send_reply]

ACADEMY_TOOLS = [*READ_ONLY_TOOL_OBJECTS, *WRITE_TOOLS, Question, Done]


def academy_tools() -> list:
    """The tools a new graph gets. Read-only ones go through MCP when USE_MCP is on."""
    if not settings.use_mcp():
        return list(ACADEMY_TOOLS)
    from email_assistant.academy.mcp_bridge import mcp_proxy_tools

    return [*mcp_proxy_tools(READ_ONLY_TOOL_OBJECTS), *WRITE_TOOLS, Question, Done]


TOOLS_PROMPT = """
1. find_demo_slots(date_from, date_to) - free demo-class slots between two dates (YYYY-MM-DD). Read-only.
2. get_plans() - the fee plans: classes per month and monthly fee. Read-only.
3. get_batch_timings() - the regular batches: days, time, level, seats left. Read-only.
4. get_workshops() - upcoming workshops: date, fee, seats left. Read-only.
5. get_tournaments() - upcoming tournaments: date, entry fee, seats left. Read-only.
6. create_demo_event(parent_email, child_first_name, start, end) - book a demo class. start and end are strings like 2026-11-07T14:00. The owner approves first.
7. reschedule_demo(parent_email, child_first_name, new_start, new_end) - move a booked demo class. The owner approves first.
8. cancel_demo(parent_email, child_first_name) - cancel a booked demo class. The owner approves first.
9. send_reply(to, subject, body) - reply to the parent. to is the parent's email address. The owner approves first.
10. Question(content) - ask the owner when you are missing information a tool cannot give.
11. Done - the task is finished.
"""
