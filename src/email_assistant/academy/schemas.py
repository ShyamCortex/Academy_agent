"""Labels, routing table and graph state for the academy graph."""

from typing import Literal, get_args

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# The one place the label list is written down. Everything else is built from it.
Label = Literal[
    "book_demo",
    "fees_plans",
    "schedule",
    "workshop_tournament",
    "reschedule_cancel",
    "sensitive",
    "ignore",
]
LABELS: tuple[str, ...] = get_args(Label)

# Where each label goes. `sensitive` has NO route to the drafter: it goes to the owner only.
DRAFTABLE_LABELS = frozenset({"book_demo", "fees_plans", "schedule", "workshop_tournament", "reschedule_cancel"})
NEVER_DRAFT_LABELS = frozenset({"sensitive"})


class RouterSchema(BaseModel):
    """Read one message from a parent and say what kind of message it is."""

    reasoning: str = Field(description="Short reasoning for the label. Do not copy the message.")
    classification: Label = Field(description="Exactly one label from the list in the instructions.")


class StateInput(TypedDict):
    # What you give the graph: one parent message.
    # Keys: "from" (sender name and contact), "text" (the message), optional "subject".
    message_input: dict


class State(MessagesState):
    message_input: dict
    classification_decision: Label
