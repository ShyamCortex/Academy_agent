"""Academy Support Agent graph (Phase 1, mock tools).

    START -> triage_router --+-- draftable label --> response_agent (draft -> owner approval -> tools)
                             +-- sensitive --------> notify_owner -> END     (no edge to response_agent)
                             +-- ignore -----------> END

Built from `email_assistant_hitl_memory.py`. What is different, and why:
- 7 parent labels instead of ignore / notify / respond.
- `sensitive` has no path to the drafter, and the drafter also refuses it at its door.
- Approval is deny-by-default: only tools on `guards.READ_ONLY_TOOLS` run without the owner.
- Memory learns reply wording only.
- Dates are read on every call. Message text is never printed or logged.
- One tool call per model turn (see `llm_call`).
"""

import logging
from typing import Any, Literal, Sequence

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Command, interrupt

from email_assistant.academy.guards import (
    WORDING_TOOLS,
    assert_draftable,
    needs_approval,
)
from email_assistant.academy.memory import get_preferences, update_preferences
from email_assistant.academy.prompts import (
    build_agent_prompt,
    default_background,
    triage_system_prompt,
    triage_user_prompt,
)
from email_assistant.academy.schemas import DRAFTABLE_LABELS, RouterSchema, State, StateInput
from email_assistant.academy.tools import ACADEMY_TOOLS, TOOLS_PROMPT
from email_assistant.models import structured_llm, tool_llm

log = logging.getLogger(__name__)


# ---- helpers -------------------------------------------------------------------------------

def parts_of(message_input: dict) -> tuple[str, str, str]:
    """(sender, subject, text) from a message dict."""
    return message_input.get("from", ""), message_input.get("subject", ""), message_input["text"]


def message_markdown(message_input: dict) -> str:
    """The parent's message as markdown, for the owner's inbox (not for logs)."""
    sender, subject, text = parts_of(message_input)
    subject_line = f"**Subject**: {subject}\n" if subject else ""
    return f"\n\n**From**: {sender}\n{subject_line}\n{text}\n\n---\n"


def tool_call_markdown(tool_call: dict) -> str:
    """A tool call shown to the owner: tool name, then each argument on its own line."""
    lines = [f"# {tool_call['name']}", ""]
    for key, value in (tool_call["args"] or {}).items():
        lines.append(f"**{key}**: {value}")
    return "\n".join(lines) + "\n"


def triage_message(message_input: dict) -> str:
    """Label one message. The graph and the triage-accuracy script both call this."""
    sender, subject, text = parts_of(message_input)
    result = _router.invoke([
        {"role": "system", "content": triage_system_prompt.format(background=default_background)},
        {"role": "user", "content": triage_user_prompt.format(sender=sender, subject=subject, text=text)},
    ])
    return result.classification


_router = structured_llm("triage", RouterSchema)


# ---- the graph -----------------------------------------------------------------------------

def build_graph(tools: Sequence[BaseTool] = ACADEMY_TOOLS) -> StateGraph:
    """Build the (uncompiled) academy graph. Tests pass extra tools to prove they need approval."""
    tools_by_name = {t.name: t for t in tools}
    # AGENT_MODEL, forced to call a tool every turn (the model ends with the Done tool)
    llm_with_tools = tool_llm("agent", list(tools), tool_choice="required")

    def triage_router(state: State) -> Command[Literal["response_agent", "notify_owner", "__end__"]]:
        label = triage_message(state["message_input"])
        log.info("triage label: %s", label)  # the label only, never the message
        if label in DRAFTABLE_LABELS:
            goto = "response_agent"
            update: dict[str, Any] = {
                "classification_decision": label,
                "messages": [{"role": "user", "content": f"Reply to this parent message: {message_markdown(state['message_input'])}"}],
            }
        elif label == "sensitive":
            # Nothing about the message goes into `messages`: that list is the drafter's input.
            goto, update = "notify_owner", {"classification_decision": label}
        elif label == "ignore":
            goto, update = END, {"classification_decision": label}
        else:
            raise ValueError(f"Invalid classification: {label}")
        return Command(goto=goto, update=update)

    def notify_owner(state: State) -> Command[Literal["__end__"]]:
        """Show a sensitive message to the owner. Whatever the owner answers, the run ends here."""
        request = {
            "action_request": {"action": "Sensitive message: needs you personally", "args": {}},
            "config": {"allow_ignore": True, "allow_respond": False, "allow_edit": False, "allow_accept": False},
            "description": message_markdown(state["message_input"]),
        }
        interrupt([request])  # the answer is deliberately not read: it cannot start a draft
        return Command(goto=END)

    def llm_call(state: State, store: BaseStore):
        """The drafting model decides which tool to call next."""
        assert_draftable(state.get("classification_decision"))  # sensitive stops here, whatever else happened
        preferences = get_preferences(store)
        response = llm_with_tools.invoke(
            [{"role": "system", "content": build_agent_prompt(TOOLS_PROMPT, preferences)}] + state["messages"]
        )
        # One tool call per turn. A node that is resumed after an interrupt runs again from its
        # top, so two approved writes in one turn could be executed twice. One at a time cannot.
        if len(response.tool_calls) > 1:
            response = response.model_copy(update={"tool_calls": response.tool_calls[:1]})
        return {"messages": [response]}

    def interrupt_handler(state: State, store: BaseStore) -> Command[Literal["llm_call", "__end__"]]:
        """Run the model's tool call. Anything not on the read-only allow-list waits for the owner."""
        result: list[Any] = []
        goto = "llm_call"
        for tool_call in state["messages"][-1].tool_calls:
            name, call_id = tool_call["name"], tool_call["id"]

            if name not in tools_by_name:  # a tool we do not have is never run
                result.append({"role": "tool", "content": f"Unknown tool {name!r}. Use only the listed tools.", "tool_call_id": call_id})
                continue

            if not needs_approval(name):  # read-only allow-list
                result.append({"role": "tool", "content": tools_by_name[name].invoke(tool_call["args"]), "tool_call_id": call_id})
                continue

            # ---- everything else asks the owner first ----
            can_accept = name != "Question"
            request = {
                "action_request": {"action": name, "args": tool_call["args"]},
                "config": {"allow_ignore": True, "allow_respond": True, "allow_edit": can_accept, "allow_accept": can_accept},
                "description": message_markdown(state["message_input"]) + tool_call_markdown(tool_call),
            }
            response = interrupt([request])[0]
            kind = response["type"]

            if kind == "accept" and can_accept:
                result.append({"role": "tool", "content": tools_by_name[name].invoke(tool_call["args"]), "tool_call_id": call_id})

            elif kind == "edit" and can_accept:
                edited_args = response["args"]["args"]
                ai_message = state["messages"][-1]
                edited_call = {"type": "tool_call", "name": name, "args": edited_args, "id": call_id}
                result.append(ai_message.model_copy(update={"tool_calls": [edited_call]}))
                result.append({"role": "tool", "content": tools_by_name[name].invoke(edited_args), "tool_call_id": call_id})
                if name in WORDING_TOOLS:
                    update_preferences(store, f"The owner edited a reply before sending. Draft: {tool_call['args']}. Edited: {edited_args}.")

            elif kind == "ignore":
                result.append({"role": "tool", "content": "The owner declined this. Stop and end the workflow.", "tool_call_id": call_id})
                goto = END

            elif kind == "response":
                feedback = response["args"]
                result.append({"role": "tool", "content": f"The owner gave feedback instead: {feedback}", "tool_call_id": call_id})
                if name in WORDING_TOOLS:
                    update_preferences(store, f"The owner gave feedback on a reply: {feedback}")

            else:
                raise ValueError(f"Invalid owner response {kind!r} for tool {name!r}")

        return Command(goto=goto, update={"messages": result})

    def should_continue(state: State) -> Literal["interrupt_handler", "__end__"]:
        """Go to the tool step, or end when the model says Done (or sends no tool call)."""
        calls = state["messages"][-1].tool_calls
        if not calls or any(call["name"] == "Done" for call in calls):
            return END
        return "interrupt_handler"

    agent = StateGraph(State)
    agent.add_node("llm_call", llm_call)
    agent.add_node("interrupt_handler", interrupt_handler)
    agent.add_edge(START, "llm_call")
    agent.add_conditional_edges("llm_call", should_continue, {"interrupt_handler": "interrupt_handler", END: END})
    response_agent = agent.compile()

    return (
        StateGraph(State, input_schema=StateInput)
        .add_node("triage_router", triage_router)
        .add_node("notify_owner", notify_owner)
        .add_node("response_agent", response_agent)
        .add_edge(START, "triage_router")
    )


overall_workflow = build_graph()
# No checkpointer or store here: `langgraph dev` / Studio adds them. Tests compile their own.
academy_agent = overall_workflow.compile()
