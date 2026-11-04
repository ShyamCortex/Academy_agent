"""Long-term memory: the agent learns reply wording only (`response_preferences`).

There is no triage memory and no calendar memory. Triage rules and calendar rules are written
in code and prompts, so feedback cannot teach the agent to treat a sensitive message as a normal one.

Values are stored as a dict, `{"preferences": "<text>"}`. LangGraph 1.x stores reject plain
strings (the upstream code stored plain strings and fails with ValueError).
"""

import logging

from langgraph.store.base import BaseStore

from email_assistant.academy.prompts import (
    MEMORY_UPDATE_INSTRUCTIONS,
    MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT,
    default_response_preferences,
)
from email_assistant.models import structured_llm
from email_assistant.schemas import UserPreferences

log = logging.getLogger(__name__)

NAMESPACE = ("academy_agent", "response_preferences")
KEY = "preferences"

memory_llm = structured_llm("agent", UserPreferences)


def get_preferences(store: BaseStore) -> str:
    """Return the saved wording preferences, saving the default the first time."""
    item = store.get(NAMESPACE, KEY)
    if item is not None:
        return item.value["preferences"]
    store.put(NAMESPACE, KEY, {"preferences": default_response_preferences})
    return default_response_preferences


def update_preferences(store: BaseStore, feedback: str) -> bool:
    """Rewrite the preferences from the owner's feedback. Returns True if they changed.

    Best effort: if the model call fails, the old preferences stay and the run goes on. This
    runs right after the owner approved an action, and an error here must never make the
    graph repeat that action.
    """
    current = get_preferences(store)
    try:
        result = memory_llm.invoke([
            {"role": "system", "content": MEMORY_UPDATE_INSTRUCTIONS.format(current_profile=current)},
            {"role": "user", "content": f"{feedback}\n\n{MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT}"},
        ])
    except Exception as exc:  # noqa: BLE001 - log the error type only, never its text
        log.warning("Could not update response preferences (%s); keeping the old ones.", type(exc).__name__)
        return False
    updated = (result.user_preferences or "").strip()
    if not updated:
        return False
    store.put(NAMESPACE, KEY, {"preferences": updated})
    return True
