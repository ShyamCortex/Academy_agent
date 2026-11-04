"""Approved replies go to a local outbox file. Nothing is emailed.

`send_reply` is a write tool: the owner approves every call (it is not on the read-only
allow-list). After approval it appends one JSON line to the outbox file. Sending real mail
(Gmail) is out of scope for the core. The file is git-ignored, and holds synthetic messages only.

Never print or log the body (hard rule 5): the outbox file is the only place it is written.
"""

import json
import threading
import uuid
from pathlib import Path

from langchain_core.tools import tool

from email_assistant.academy import dates, settings

SOURCE = "local outbox (nothing is emailed)"
_lock = threading.Lock()


def outbox_path() -> Path:
    return Path(settings.env_or(settings.OUTBOX_FILE_VAR, settings.DEFAULT_OUTBOX_FILE))


def read_outbox(path: Path | None = None) -> list[dict]:
    """All queued replies, oldest first. An outbox that does not exist yet is empty."""
    path = path or outbox_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@tool
def send_reply(to: str, subject: str, body: str) -> str:
    """Send a reply to the parent. to is the parent's email address."""
    if "@" not in to or not body.strip():
        return json.dumps({"source": SOURCE, "queued": False, "error": "to must be an email address and body must not be empty."})
    record = {"id": uuid.uuid4().hex[:12], "at": dates.now_ist().isoformat(timespec="seconds"),
              "to": to.strip(), "subject": subject.strip(), "body": body}
    path = outbox_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as outbox:
            outbox.write(json.dumps(record, ensure_ascii=False) + "\n")
    return json.dumps({"source": SOURCE, "queued": True, "outbox_id": record["id"]})
