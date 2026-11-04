"""Call the read-only MCP server from the (synchronous) graph.

The graph's tool step is synchronous, and the MCP client is async. `McpReadClient` keeps one
background thread with its own event loop and one open MCP session, and turns a call into a
plain function call: `client.call("get_plans", {})` returns the tool's text.

`mcp_proxy_tools` makes LangChain tools with the SAME name, description and arguments as the
in-process read-only tools, so the model, the graph and the tests cannot tell the difference.

By default the server is started as a child process (stdio). It gets only the settings it needs
(the anon key, the calendar id, the token file): not the model keys and not the LangSmith key.
Tests connect to an in-process server instead (`in_process_client`).
"""

import asyncio
import atexit
import concurrent.futures
import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from langchain_core.tools import BaseTool, StructuredTool
from mcp import Client, StdioServerParameters

from email_assistant.academy import settings
from email_assistant.academy.guards import READ_ONLY_TOOLS

# The only environment variables the MCP server process receives.
SERVER_ENV_VARS = (
    settings.SUPABASE_URL_VAR, settings.SUPABASE_ANON_KEY_VAR,
    settings.CALENDAR_ID_VAR, settings.CREDENTIALS_FILE_VAR, settings.TOKEN_FILE_VAR,
    settings.EMBEDDING_MODEL_VAR, settings.EMBEDDING_CACHE_VAR,  # FAQ search embeds the question inside the server
)


class McpError(RuntimeError):
    """The MCP server could not be started, or a tool call through it failed."""


class McpReadClient:
    """A synchronous client for one MCP server. `target` is anything `mcp.Client` accepts."""

    def __init__(self, target: Any, *, timeout: float = 60.0):
        self._target = target
        self._timeout = timeout
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # ---- background thread ----
    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        try:
            # One task opens and closes the session (anyio requires that); calls are fed through the queue.
            async with Client(self._target) as client:
                self._ready.set()
                while True:
                    item = await self._queue.get()
                    if item is None:
                        return
                    name, arguments, future = item
                    try:
                        future.set_result(await client.call_tool(name, arguments))
                    except BaseException as exc:  # handed back to the caller's thread
                        future.set_exception(exc)
        except BaseException as exc:
            if not self._ready.is_set():
                self._start_error = exc
        finally:
            self._ready.set()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._start_error = None
            self._thread = threading.Thread(target=self._run, name="academy-mcp-client", daemon=True)
            self._thread.start()
            if not self._ready.wait(self._timeout):
                raise McpError("The MCP server did not start in time.")
            if self._start_error is not None:
                raise McpError(f"The MCP server could not be started: {type(self._start_error).__name__}.") from None

    # ---- public ----
    def call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call one read-only tool and return its text. Any other tool name is refused here too."""
        if name not in READ_ONLY_TOOLS:
            raise McpError(f"{name!r} is not a read-only tool, so it is not called over MCP.")
        self._ensure_started()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, arguments or {}, future))
        try:
            result = future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            raise McpError(f"The MCP tool {name!r} did not answer in time.") from None
        text = "".join(part.text for part in result.content if getattr(part, "type", "") == "text")
        if result.is_error:
            raise McpError(text or f"The MCP tool {name!r} failed.")
        return text

    def list_tool_names(self) -> list[str]:
        """Names the server offers (used by tests and the laptop check)."""
        async def names() -> list[str]:
            async with Client(self._target) as client:
                return sorted(tool.name for tool in (await client.list_tools()).tools)
        return asyncio.run(names())

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive() and self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join(timeout=10)


# ---- which client the proxies use ---------------------------------------------------------------

def stdio_target() -> StdioServerParameters:
    """Start `python -m email_assistant.academy.mcp_server` with only the settings it needs."""
    env = {name: os.environ[name] for name in SERVER_ENV_VARS if name in os.environ}
    return StdioServerParameters(command=sys.executable, args=["-m", "email_assistant.academy.mcp_server"], env=env, cwd=os.getcwd())


def in_process_client() -> McpReadClient:
    """A client wired straight to an in-process server (tests): same tools, no subprocess."""
    from email_assistant.academy.mcp_server import build_server

    return McpReadClient(build_server())


_override: McpReadClient | None = None
_cached: McpReadClient | None = None
_lock = threading.Lock()


def get_mcp_client() -> McpReadClient:
    """The client the proxies use: a test's client if one is set, else the stdio server (started on first use)."""
    global _cached
    if _override is not None:
        return _override
    with _lock:
        if _cached is None:
            _cached = McpReadClient(stdio_target())
            atexit.register(_cached.close)
        return _cached


@contextmanager
def use_mcp_client(client: McpReadClient) -> Iterator[McpReadClient]:
    """Temporarily use another client (tests)."""
    global _override
    previous, _override = _override, client
    try:
        yield client
    finally:
        _override = previous


def _proxy(tool: BaseTool) -> StructuredTool:
    name = tool.name

    def call(**arguments: Any) -> str:
        return get_mcp_client().call(name, arguments)

    return StructuredTool(name=name, description=tool.description, args_schema=tool.args_schema, func=call)


def mcp_proxy_tools(tools: Sequence[BaseTool]) -> list[StructuredTool]:
    """Tools that look exactly like `tools` but run over MCP. Only read-only tools can be proxied."""
    blocked = [t.name for t in tools if t.name not in READ_ONLY_TOOLS]
    if blocked:
        raise McpError(f"Only read-only tools can go over MCP. Refused: {blocked}")
    return [_proxy(t) for t in tools]


def main(argv: list[str] | None = None) -> int:
    """Laptop check: start the MCP server as a child process, list its tools, call one of them.

        python -m email_assistant.academy.mcp_bridge get_plans
    """
    import argparse

    from dotenv import load_dotenv

    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Call a read-only tool through the MCP server.")
    parser.add_argument("tool", nargs="?", default="get_plans", help="a read-only tool that needs no arguments")
    args = parser.parse_args(argv)
    client = McpReadClient(stdio_target())
    try:
        print("The MCP server offers:", ", ".join(client.list_tool_names()))
        print(client.call(args.tool, {}))
        return 0
    except McpError as exc:
        print(f"Failed: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
