"""Safety rules that live in code. Prompts and memory cannot change what is written here.

1. Deny by default: a tool runs without the owner's approval only if its exact name is in
   READ_ONLY_TOOLS. Every other tool, including any tool added in the future, asks first.
2. A `sensitive` message never reaches the drafter (no graph edge, plus a check at the drafter's door).
"""

from email_assistant.academy.schemas import DRAFTABLE_LABELS

# Tools that only look things up and change nothing. Adding a name here is a deliberate act:
# it removes the owner's approval for that tool. Exact match, case-sensitive.
READ_ONLY_TOOLS = frozenset({
    "find_demo_slots",
    "get_plans",
    "get_batch_timings",
    "get_workshops",
    "get_tournaments",
    "search_knowledge",
})

# The only write tool whose edits teach the agent wording (response_preferences memory).
WORDING_TOOLS = frozenset({"send_reply"})

# Tool calls that are not real tools: `Done` ends the run, `Question` asks the owner.
SIGNAL_TOOLS = frozenset({"Done", "Question"})


def needs_approval(tool_name: str) -> bool:
    """True unless the tool is on the read-only allow-list."""
    return tool_name not in READ_ONLY_TOOLS


class SensitiveBlockedError(RuntimeError):
    """A message that must not be drafted (sensitive, ignored, or unlabelled) reached the drafter."""


def assert_draftable(classification: str | None) -> None:
    """Raise unless a message with this label may be drafted. Called at the drafter's entrance.

    Deny by default: only the five parent-question labels pass. `sensitive`, `ignore`, a
    missing label and any unknown label all stop here.
    """
    if classification not in DRAFTABLE_LABELS:
        raise SensitiveBlockedError(
            f"Refusing to draft: classification is {classification!r}. "
            "Sensitive messages go to the owner only."
        )
