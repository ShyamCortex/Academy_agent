"""A small read-only MCP server (Model Context Protocol) for the academy lookups.

MCP is a standard way to offer tools, so any MCP client can call them. This server offers
EXACTLY the tools on `guards.READ_ONLY_TOOLS`, built from the same tool objects the graph uses.
Write tools (booking, rescheduling, cancelling, replying) are never offered: an MCP client
would skip the owner's approval (hard rule 1).

Run it (stdio transport):   python -m email_assistant.academy.mcp_server
The agent starts it for you when USE_MCP=1 (see `mcp_bridge.py`).

It reads the same settings as the in-process tools: ACADEMY_SUPABASE_URL, ACADEMY_SUPABASE_ANON_KEY,
GOOGLE_CALENDAR_ID and the Google token file. Nothing is written to stdout except MCP messages.
"""

import functools

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from email_assistant.academy.guards import READ_ONLY_TOOLS
from email_assistant.academy.settings import AcademyConfigError
from email_assistant.academy.tools import READ_ONLY_TOOL_OBJECTS

INSTRUCTIONS = "Read-only lookups for a children's chess academy: free demo slots, fee plans, batch timings, workshops, tournaments."


def _expose(tool):
    """The tool's function, except that a settings problem reaches the client with its message.

    MCP hides the text of unexpected errors from clients (they see only "Error executing tool ..."). A
    missing or wrong setting is expected and the message names the variable to fix, so it is passed on.
    """
    @functools.wraps(tool.func)
    def run(*args, **kwargs):
        try:
            return tool.func(*args, **kwargs)
        except AcademyConfigError as exc:
            raise ToolError(str(exc)) from None

    return run


def build_server() -> MCPServer:
    """A server with the read-only tools and nothing else."""
    server = MCPServer("academy-read-only", instructions=INSTRUCTIONS)
    for tool in READ_ONLY_TOOL_OBJECTS:
        if tool.name not in READ_ONLY_TOOLS:  # never expose a tool the allow-list does not name
            raise RuntimeError(f"{tool.name!r} is not on the read-only allow-list, so it cannot be offered over MCP.")
        server.add_tool(_expose(tool), name=tool.name, description=tool.description, annotations=ToolAnnotations(read_only_hint=True))
    return server


def main() -> None:
    load_dotenv(".env")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
