"""Environment settings for the real tools. Missing settings raise an error that names the variable."""

import os

# Test Supabase project (read-only, anon key). Never the real EduChess project (hard rule 4).
SUPABASE_URL_VAR = "ACADEMY_SUPABASE_URL"
SUPABASE_ANON_KEY_VAR = "ACADEMY_SUPABASE_ANON_KEY"
# Google test calendar. No default on purpose: we never fall back to a personal "primary" calendar.
CALENDAR_ID_VAR = "GOOGLE_CALENDAR_ID"
CREDENTIALS_FILE_VAR = "GOOGLE_CREDENTIALS_FILE"
TOKEN_FILE_VAR = "GOOGLE_TOKEN_FILE"
OUTBOX_FILE_VAR = "ACADEMY_OUTBOX_FILE"
USE_MCP_VAR = "USE_MCP"
# FAQ search: which local embedding model turns text into vectors ("provider and model names come from env vars").
EMBEDDING_MODEL_VAR = "EMBEDDING_MODEL"
EMBEDDING_CACHE_VAR = "FASTEMBED_CACHE_PATH"  # optional: where the downloaded model is kept

DEFAULT_CREDENTIALS_FILE = ".secrets/academy_google_credentials.json"
DEFAULT_TOKEN_FILE = ".secrets/academy_google_token.json"
DEFAULT_OUTBOX_FILE = "outbox/academy_outbox.jsonl"


class AcademyConfigError(RuntimeError):
    """A setting the tools need is missing or wrong. The message says what to do."""


def require_env(name: str) -> str:
    """Return a required environment variable, or raise an error that names it."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise AcademyConfigError(f"{name} is not set. Put {name}=... in your .env file (see .env.example).")
    return value


def env_or(name: str, default: str) -> str:
    """An optional environment variable with a default."""
    return os.environ.get(name, "").strip() or default


def use_mcp() -> bool:
    """True when USE_MCP is 1, true or yes: the read-only tools are then called through MCP."""
    return os.environ.get(USE_MCP_VAR, "").strip().lower() in {"1", "true", "yes"}
