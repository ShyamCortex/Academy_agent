"""Read-only client for the TEST Supabase project (hard rule 4).

Three layers keep it read-only, so one mistake is not enough to write:
1. The only public methods are `select` and `match_faq` (FAQ search), and both only send GET.
2. The HTTP transport refuses any request that is not GET, even if someone later adds a call.
3. It accepts only an anon (public) key. A key that is not provably an anon key is refused.
   In the database, the anon role has SELECT permission only (`supabase/academy_test_schema.sql`).

Settings (in `.env`): ACADEMY_SUPABASE_URL and ACADEMY_SUPABASE_ANON_KEY of the separate test project.
There is no other key and no other project. The FAQ loader (a script in the top-level `scripts/` folder, outside this package) is
the only writer. It is a different program, run on the owner's laptop, and nothing in `src/` imports it.
"""

import base64
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlsplit

import httpx

from email_assistant.academy import settings

# Tables the agent may read. Any other table name is refused before a request is made.
READABLE_TABLES = frozenset({"plans", "batches", "workshops", "tournaments"})
# The one database function the agent may call: FAQ search (`supabase/academy_faq_rag.sql`). It is declared
# `stable`, so PostgREST accepts it over GET and runs it in a read-only transaction.
FAQ_MATCH_FUNCTION = "match_faq"
MAX_FAQ_MATCHES = 5

PUBLISHABLE_PREFIX = "sb_publishable_"  # Supabase's new-style public key
RETRY_STATUS = {429, 500, 502, 503, 504}


class DataSourceError(RuntimeError):
    """The data source could not be read. The message holds the status only, never row data."""


class ReadOnlyViolation(RuntimeError):
    """Something tried to send a request that is not a plain read."""


def _jwt_role(key: str) -> str | None:
    """The `role` inside a JWT-style key, or None. The signature is not checked: we only read the label."""
    parts = key.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def check_anon_key(key: str) -> None:
    """Allow only a public key: a `sb_publishable_` key, or a JWT whose role is exactly `anon`."""
    if key.startswith(PUBLISHABLE_PREFIX) or _jwt_role(key) == "anon":
        return
    raise settings.AcademyConfigError(
        f"{settings.SUPABASE_ANON_KEY_VAR} must be the test project's anon (public) key. "
        "This key is not one, so it is refused. Never put a private key here."
    )


def check_url(url: str) -> str:
    """Require https (plain http only for a local test server). Returns the URL without a trailing slash."""
    parts = urlsplit(url)
    local = parts.hostname in {"localhost", "127.0.0.1"}
    if not parts.hostname or parts.scheme not in {"https", "http"} or (parts.scheme == "http" and not local):
        raise settings.AcademyConfigError(f"{settings.SUPABASE_URL_VAR} must be an https URL like https://<ref>.supabase.co.")
    return url.rstrip("/")


class _GetOnlyTransport(httpx.BaseTransport):
    """Wraps a transport and refuses everything except GET."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise ReadOnlyViolation(f"Blocked a {request.method} request: this client is read-only.")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class ReadOnlyRest:
    """Reads rows from the PostgREST API of the test project."""

    def __init__(
        self,
        url: str,
        anon_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 2,
    ):
        check_anon_key(anon_key)
        headers = {"apikey": anon_key, "Accept": "application/json"}
        if _jwt_role(anon_key):  # old-style keys are JWTs and also go in Authorization; new-style keys are not JWTs
            headers["Authorization"] = f"Bearer {anon_key}"
        self._http = httpx.Client(
            base_url=check_url(url) + "/rest/v1/",
            headers=headers,
            transport=_GetOnlyTransport(transport or httpx.HTTPTransport()),
            timeout=10.0,
        )
        self._sleep = sleep
        self._attempts = attempts

    @classmethod
    def from_env(cls) -> "ReadOnlyRest":
        return cls(settings.require_env(settings.SUPABASE_URL_VAR), settings.require_env(settings.SUPABASE_ANON_KEY_VAR))

    def select(
        self, table: str, columns: str, *, filters: dict[str, str] | None = None, order: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """GET rows of one table. `filters` use PostgREST syntax, e.g. {"event_date": "gte.2026-11-07"}."""
        if table not in READABLE_TABLES:
            raise ValueError(f"Table {table!r} is not readable by the agent.")
        params = {"select": columns, **(filters or {}), "limit": str(limit)}
        if order:
            params["order"] = order
        return self._get(table, params)

    def match_faq(self, embedding: Sequence[float], count: int = 3) -> list[dict[str, Any]]:
        """The FAQ chunks closest to `embedding`, best first (a GET of the `match_faq` function).

        This is the only function the client can call. The name is fixed here on purpose: there is no
        general "call any function" method, because a database function can write.
        """
        if not 1 <= count <= MAX_FAQ_MATCHES:
            raise ValueError(f"count must be between 1 and {MAX_FAQ_MATCHES}.")
        vector = "[" + ",".join(f"{float(x):.5f}" for x in embedding) + "]"
        return self._get(f"rpc/{FAQ_MATCH_FUNCTION}", {"query_embedding": vector, "match_count": str(count)})

    def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        for attempt in range(self._attempts):
            last = attempt == self._attempts - 1
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError:
                if last:
                    raise DataSourceError("The data source could not be reached.") from None
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code in RETRY_STATUS and not last:
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code != 200:
                raise DataSourceError(f"The data source answered with HTTP {response.status_code}.")
            rows = response.json()
            if not isinstance(rows, list):
                raise DataSourceError("The data source sent an unexpected answer.")
            return rows
        raise DataSourceError("The data source could not be read.")  # only if attempts < 1


_override: ReadOnlyRest | None = None
_cached: ReadOnlyRest | None = None
_lock = threading.Lock()


def get_data_client() -> ReadOnlyRest:
    """The client the tools use: a test's client if one is set, else one built from the environment (once)."""
    global _cached
    if _override is not None:
        return _override
    with _lock:
        if _cached is None:
            _cached = ReadOnlyRest.from_env()
        return _cached


@contextmanager
def use_data_client(client: ReadOnlyRest) -> Iterator[ReadOnlyRest]:
    """Temporarily use another client (tests). A plain module variable, so MCP worker threads see it too."""
    global _override
    previous, _override = _override, client
    try:
        yield client
    finally:
        _override = previous
