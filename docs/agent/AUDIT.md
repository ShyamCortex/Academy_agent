# Audit: agents-from-scratch base repo

Audited on 2026-11-03 at commit `603fc7a` (same as `main`).
Base: a standalone copy of `langchain-ai/agents-from-scratch` (MIT, Copyright LangChain, Inc.).

Purpose: know exactly what the code does today before turning it into the
EduChess **Academy Support Agent**.

How this was checked:
- Read every file under `src/`, `tests/`, `.github/`, plus the root config files.
- Installed dependencies with `uv sync --extra dev` in the cloud container (Python 3.13.18).
- Imported each module with **no API keys** set, to see what breaks.
- Anything not proven by code or by running it is marked **UNKNOWN**.

---

## 1. Repo layout

```
Academy_agent/
├── .env.example                  # OPENAI_API_KEY, LANGSMITH_* only
├── .github/workflows/
│   ├── claude.yml                # @claude mention bot
│   └── claude-code-review.yml    # auto PR review
├── AGENTS.md / CLAUDE.md         # agent instructions (near-identical)
├── LICENSE                       # MIT, LangChain, Inc.
├── README.md                     # upstream tutorial README
├── langgraph.json                # graphs exposed to `langgraph dev`
├── pyproject.toml / uv.lock
├── notebooks/                    # 5 teaching notebooks + img/ + test_tools.py
├── src/email_assistant/
│   ├── langgraph_101.py          # toy graph, 1 tool
│   ├── email_assistant.py        # triage + agent, no HITL
│   ├── email_assistant_hitl.py   # + interrupts (human approval)
│   ├── email_assistant_hitl_memory.py        # + long-term memory (store)
│   ├── email_assistant_hitl_memory_gmail.py  # + real Gmail/Calendar tools
│   ├── cron.py                   # graph that runs Gmail ingest on a schedule
│   ├── prompts.py                # all agent + triage + memory prompts
│   ├── schemas.py                # RouterSchema, State, StateInput, UserPreferences
│   ├── configuration.py          # unused placeholder Configuration dataclass
│   ├── utils.py                  # parsing / formatting helpers
│   ├── tools/
│   │   ├── base.py               # get_tools(), get_tools_by_name()
│   │   ├── default/              # mock email + calendar tools, tool-list prompts
│   │   └── gmail/                # real Gmail + Google Calendar tools, ingest, OAuth setup
│   └── eval/
│       ├── email_dataset.py      # 16 labelled emails (Python literals)
│       ├── evaluate_triage.py    # LangSmith evaluate() script (runs on import)
│       └── prompts.py            # judge prompts
└── tests/
    ├── conftest.py               # --agent-module option
    ├── run_all_tests.py          # wrapper: runs test_response.py per module
    ├── test_response.py          # tool-call + LLM-judge tests
    └── test_notebooks.py         # executes every notebook
```

There is no `docs/` folder, no `Dockerfile`, no FastAPI code, no MCP code, no RAG code.

---

## 2. Which graph file to build on

**Recommendation: copy `src/email_assistant/email_assistant_hitl_memory.py` into a new academy module.**

| File | Triage | HITL | Memory | Real tools | Verdict |
|---|---|---|---|---|---|
| `email_assistant.py` | yes | no | no | no | Too simple: sends with no approval. |
| `email_assistant_hitl.py` | yes | yes | no | no | Good, but no learning. |
| `email_assistant_hitl_memory.py` | yes | yes | yes | no (mocks) | **Base.** All the wiring we need, mock tools, testable with an in-memory store. |
| `email_assistant_hitl_memory_gmail.py` | yes | yes | yes | Gmail + Calendar | Same graph as above; differs only in tool names, `parse_gmail`, and a final `mark_as_read_node`. Use it as a **reference** for real Google API calls. |

The diff between the memory file and the Gmail file is small (checked with `diff`):
tool names (`write_email` → `send_email_tool`, `schedule_meeting` → `schedule_meeting_tool`),
`parse_email` → `parse_gmail`, and an extra `mark_as_read_node` before `END`.

Why a copy and not an edit in place: the upstream files stay runnable and comparable,
and the academy diff stays easy to review. Trade-off: some duplicated graph code.

---

## 3. Triage labels and prompts

### Where labels are defined

The label set is hard-coded in **four** places. All must change together.

| Place | File:line | What it holds |
|---|---|---|
| Structured-output schema | `src/email_assistant/schemas.py:5-15` | `RouterSchema.classification: Literal["ignore","respond","notify"]` plus a `reasoning` field |
| Graph state | `src/email_assistant/schemas.py:21-24` | `State.classification_decision: Literal["ignore","respond","notify"]` |
| Routing code | `email_assistant_hitl_memory.py:118-151` | `if/elif` on `"respond"` → `response_agent`, `"ignore"` → `END`, `"notify"` → `triage_interrupt_handler`, else `ValueError` |
| Triage prompt text | `src/email_assistant/prompts.py:4-25` | "Categorize each email into one of three categories: IGNORE / NOTIFY / RESPOND" |
| Unused tool | `tools/default/email_tools.py:12-15` | `triage_email(category: Literal[...])` — exported but not used by any graph |

### How the triage call works

1. `triage_router` (`email_assistant_hitl_memory.py:79`) parses the email with `parse_email`.
2. It loads triage rules from memory: `get_memory(store, ("email_assistant","triage_preferences"), default_triage_instructions)`.
3. It fills `triage_system_prompt` with `{background}` and `{triage_instructions}`.
4. It calls `llm_router = llm.with_structured_output(RouterSchema)`.
   - structured output — the model must return JSON that fits a Pydantic class, here `RouterSchema`.
5. It returns a `Command(goto=..., update=...)`.
   - `Command` — a LangGraph object that both updates state and picks the next node.

### Prompt inventory (`src/email_assistant/prompts.py`)

| Name | Line | Used by | Placeholders |
|---|---|---|---|
| `triage_system_prompt` | 4 | all graphs | `{background}`, `{triage_instructions}` |
| `triage_user_prompt` | 28 | all graphs | `{author}`, `{to}`, `{subject}`, `{email_thread}` |
| `agent_system_prompt` | 37 | `email_assistant.py` | `{tools_prompt}`, `{background}`, `{response_preferences}`, `{cal_preferences}` |
| `agent_system_prompt_hitl` | 74 | `email_assistant_hitl.py` | same |
| `agent_system_prompt_hitl_memory` | 113 | memory + Gmail graphs | same |
| `default_background` | 151 | all | "I'm Lance, a software engineer at LangChain." |
| `default_response_preferences` | 156 | all | — |
| `default_cal_preferences` | 182 | all | — |
| `default_triage_instructions` | 187 | all | — |
| `MEMORY_UPDATE_INSTRUCTIONS` (+ `_REINFORCEMENT`) | 214, 275 | memory graphs | `{namespace}`, `{current_profile}` |

Tool lists shown to the model are plain strings in
`tools/default/prompt_templates.py` (`STANDARD_/AGENT_/HITL_/HITL_MEMORY_TOOLS_PROMPT`)
and `tools/gmail/prompt_templates.py` (`GMAIL_TOOLS_PROMPT`). They are **not** generated
from the real tool schemas, so they can drift from the code.

**Bug to fix later:** the agent prompts insert today's date with
`datetime.now()` at **import time** (`prompts.py:54, 92, 131`).
A long-running server would keep using the day it started.
Booking demo classes needs the real current date, in `Asia/Kolkata`.

---

## 4. Every tool and its schema

Checked by importing `get_tools(include_gmail=True)` and printing each tool's args.

### Default (mock) tools — `src/email_assistant/tools/default/`

| Tool | File:line | Args (type) | What it really does |
|---|---|---|---|
| `write_email` | `email_tools.py:5` | `to: str, subject: str, content: str` | Returns the string `"Email sent to ..."`. Sends nothing. |
| `triage_email` | `email_tools.py:12` | `category: Literal["ignore","notify","respond"]` | Returns a string. Not used by any graph. |
| `Done` | `email_tools.py:17` | `done: bool` | Pydantic class used as a "finished" signal. Never executed; `should_continue` ends the graph when it sees it. |
| `Question` | `email_tools.py:22` | `content: str` | Pydantic class. Never executed; the HITL handler turns it into a question for the human. |
| `schedule_meeting` | `calendar_tools.py:5` | `attendees: list[str], subject: str, duration_minutes: int, preferred_day: datetime, start_time: int` | Returns a formatted string. Books nothing. |
| `check_calendar_availability` | `calendar_tools.py:13` | `day: str` | Always returns `"Available times on {day}: 9:00 AM, 2:00 PM, 4:00 PM"`. |

### Gmail tools — `src/email_assistant/tools/gmail/gmail_tools.py`

| Tool | Line | Args (Pydantic schema) | Google API used |
|---|---|---|---|
| `fetch_emails_tool` | 455 | `email_address: str`, `minutes_since: int = 30` | Gmail `users.messages.list/get` |
| `send_email_tool` | 599 | `email_id: str`, `response_text: str`, `email_address: str`, `additional_recipients: list[str] \| None` | Gmail send (reply in thread) |
| `check_calendar_tool` | 787 | `dates: list[str]` (DD-MM-YYYY) | Calendar `events.list` on `primary`, whole day in **UTC** (`T00:00:00Z`–`T23:59:59Z`) |
| `schedule_meeting_tool` | 897 | `attendees, title, start_time, end_time` (ISO), `organizer_email`, `timezone = "America/Los_Angeles"` | Calendar `events.insert` on `primary` with `sendUpdates="all"` (emails attendees) |
| `mark_as_read` | helper | `email_id` | Gmail `messages.modify` (remove UNREAD) |

Credential lookup (`gmail_tools.py:64-130`): passed token → `GMAIL_TOKEN` env var →
`src/email_assistant/tools/gmail/.secrets/token.json`. OAuth scopes requested by
`tools/gmail/setup_gmail.py:44-46`: `gmail.modify` and `calendar`.

Things to change for EduChess:
- Day window is UTC, not IST. A slot at 9 AM IST is 03:30 UTC — fine here, but evening classes cross days in other zones. Use `Asia/Kolkata` explicitly.
- Default timezone `America/Los_Angeles` must become `Asia/Kolkata`.
- It lists events, it does not compute free slots. Our "find slots" tool should use the Calendar **freebusy** query plus academy rules (class hours, demo length).

### Tool registry — `src/email_assistant/tools/base.py`

`get_tools(tool_names=None, include_gmail=False)` builds a name → tool dict and
returns the subset asked for. `get_tools_by_name()` returns `{tool.name: tool}`.
New academy tools can be registered here, or in a separate academy registry (cleaner).

---

## 5. How the mock calendar works

There is no calendar state at all.

- `check_calendar_availability(day)` returns the same three times for any day.
- `schedule_meeting(...)` formats a sentence with `preferred_day.strftime(...)` and returns it.
- Nothing is stored, so a test cannot check "was the slot really free?" or "was it booked once?".

The Gmail version has a fallback too: if the Google client libraries fail to import
(`GMAIL_API_AVAILABLE = False`), `get_calendar_events` returns fixed fake events and
`send_calendar_invite` logs and returns `True` (`gmail_tools.py:651-660, 850-855`).
The libraries are installed by `pyproject.toml`, so in practice this fallback rarely triggers.

What we need instead: an in-memory fake calendar (a list of events with start/end in IST)
that the tests can inspect, behind the same interface as the real Google Calendar tools.

---

## 6. HITL and memory wiring

### HITL (human-in-the-loop) — `email_assistant_hitl_memory.py`

- interrupt — LangGraph pauses the graph, saves state in the checkpointer, and waits for a `Command(resume=...)`.
- checkpointer — the store of paused graph state, keyed by `thread_id`.

Graph shape (verified by printing `get_graph().nodes`):

```
START → triage_router ─┬─ respond ─────────────→ response_agent
                       ├─ notify → triage_interrupt_handler ─┬─ owner "response" → response_agent
                       │                                      └─ owner "ignore"   → END
                       └─ ignore ──────────────→ END

response_agent (subgraph):
START → llm_call ─┬─ tool call "Done" → END
                  └─ any other tool call → interrupt_handler → llm_call (loop)
```

`interrupt_handler` (line 249):
- `hitl_tools = ["write_email", "schedule_meeting", "Question"]` (line 262).
- Any tool **not** in that list runs immediately with no approval (line 265-271). Today that is only `check_calendar_availability` (read-only, OK).
- For HITL tools it calls `interrupt([request])[0]`. The request uses the **Agent Inbox** format: `action_request{action,args}`, `config{allow_ignore, allow_respond, allow_edit, allow_accept}`, `description` (markdown).
- Owner answers and effects:

| Owner answer | `write_email` | `schedule_meeting` | `Question` |
|---|---|---|---|
| `accept` | run tool with model's args | run tool | not allowed |
| `edit` | run tool with edited args; update `response_preferences` memory | run tool with edited args; update `cal_preferences` memory | not allowed |
| `ignore` | don't run; go to END; update `triage_preferences` | same | same |
| `response` (feedback) | don't run; feed text back to model; update `response_preferences` | feed back; update `cal_preferences` | feed answer back; no memory update |

`triage_interrupt_handler` (line 155) shows `notify` emails to the owner with only
`ignore` / `respond` allowed. If the owner picks "respond", the email goes to
`response_agent`, which **drafts a reply**. For our `sensitive` label this path must be
closed in code: sensitive messages are never drafted.

`should_continue` (line 459) only checks the **first** tool call, and returns `None`
(a graph error) if the model replies with plain text and no tool call. The code relies on
`tool_choice="required"` to prevent that. Whether Gemini honours `"required"` through
`langchain-google-genai` is **UNKNOWN** — must be tested on the laptop.

### Memory

- store — LangGraph's long-term key-value store (`BaseStore`), separate from the checkpointer.
- Three namespaces, each with one key `"user_preferences"` holding a plain string:
  - `("email_assistant", "triage_preferences")` — starts as `default_triage_instructions`
  - `("email_assistant", "response_preferences")` — starts as `default_response_preferences`
  - `("email_assistant", "cal_preferences")` — starts as `default_cal_preferences`
- `get_memory` (line 30) reads, or writes the default on first use.
- `update_memory` (line 57) asks an LLM (`UserPreferences` structured output) to rewrite the profile from the feedback, then `store.put(...)`.
- It creates a **new** `init_chat_model("openai:gpt-4.1")` on every call (line 69).

Risk for us: memory is free text the model rewrites. Owner feedback could slowly teach the
triage prompt to "respond" to a sensitive message. Hard rules must live in **code**, not in
prompts or memory.

### Where the checkpointer and store come from

- Module level: `email_assistant = overall_workflow.compile()` — **no** checkpointer, **no** store (line 501). This works only under `langgraph dev` / LangGraph Platform, which inject both.
- Tests: `tests/test_response.py:186-195` compiles with `MemorySaver()` and `InMemoryStore()`.
- A FastAPI service must compile with its own persistent checkpointer + store. Which one (Postgres, SQLite) is **UNKNOWN / to decide in Phase 6**.

### Gmail ingestion

`tools/gmail/run_ingest.py` fetches mail and sends each email to a running LangGraph
server with `langgraph_sdk.get_client(url).runs.create(...)`. The LangGraph thread id is
an MD5-derived UUID of the Gmail thread id (lines 147-152), so replies in one Gmail thread
reuse one graph thread. `cron.py` wraps this as a scheduled graph.

Privacy note: `utils.parse_gmail` prints the whole incoming email to stdout
(`utils.py:156-157`). With parent messages that leaks child data into logs.

---

## 7. Where models are chosen, and what must change for Gemini + Groq

### Every model call today

| File:line | Call | Role |
|---|---|---|
| `langgraph_101.py:14` | `init_chat_model("openai:gpt-4.1", temperature=0)` | toy |
| `email_assistant.py:21, 25` | `init_chat_model("openai:gpt-4.1", temperature=0.0)` ×2 | triage, agent (`tool_choice="any"`) |
| `email_assistant_hitl.py:22, 26` | same ×2 | triage, agent (`tool_choice="required"`) |
| `email_assistant_hitl_memory.py:23, 27, 69` | same ×3 | triage, agent (`"required"`), memory update |
| `email_assistant_hitl_memory_gmail.py:24, 28, 70` | same ×3 | triage, agent (`"required"`), memory update |
| `tests/test_response.py:150` | `init_chat_model("openai:gpt-4o")` | LLM judge |
| `notebooks/*.ipynb` | `openai:gpt-4.1` (8×), `openai:gpt-4o` (1×) | teaching |

`init_chat_model` — LangChain helper that turns a `"provider:model"` string into a chat model object.

Key facts (verified by running):
- All models are created **at import time**. Importing any graph module with no `OPENAI_API_KEY` raises `openai.OpenAIError: Missing credentials`.
- So `pytest tests/test_response.py` fails at **collection** without a key (verified: "1 error during collection").
- `src/email_assistant/configuration.py` exists but is not used anywhere.
- No retry / backoff anywhere (only each provider client's built-in default retries).

### Exactly what must change

1. **Dependencies** (`pyproject.toml`): add `langchain-google-genai` and `langchain-groq`. Neither is installed today (verified). Re-lock `uv.lock`.
2. **One model factory** (new `src/email_assistant/models.py`): read the model string per role from env vars, e.g.
   - `TRIAGE_MODEL=google_genai:<flash-lite model id>`
   - `AGENT_MODEL=google_genai:<flash model id>` (drafting, tool use, memory update)
   - `JUDGE_MODEL=groq:<open model id>`
   and wrap each model with retry + exponential backoff for HTTP 429 (rate limit).
3. **Replace the 12 hard-coded `init_chat_model("openai:...")` calls** in `src/` and `tests/` with the factory. (Notebooks: leave as upstream teaching material; decide later.)
4. **Make model creation swappable for tests** so a fake model can be injected with no key.
5. **`.env.example`**: add `GOOGLE_API_KEY`, `GROQ_API_KEY`, `TRIAGE_MODEL`, `AGENT_MODEL`, `JUDGE_MODEL`; keep `LANGSMITH_*`.
   - The provider prefixes `google_genai` and `groq` are the ones `init_chat_model` documents; the env var names each SDK reads (`GOOGLE_API_KEY` vs `GEMINI_API_KEY`, `GROQ_API_KEY`) — confirm against current docs in Phase 1a.
6. **Check behaviour differences** on the laptop (UNKNOWN until run with real keys):
   - `bind_tools(..., tool_choice="required")` and `"any"` with Gemini.
   - `with_structured_output(RouterSchema)` with a `Literal` field on Flash-Lite.
   - `with_structured_output(CriteriaGrade)` on the chosen Groq model.
   - Pydantic-class tools (`Done`, `Question`) as Gemini function declarations.

### Model names

Must be checked in the official docs at implementation time; not hard-coded.
A web search on 2026-11-03 showed candidates, but the snapshots disagreed on dates — **UNKNOWN until verified**:
- Gemini models page: <https://ai.google.dev/gemini-api/docs/models> (mentions `gemini-3.5-flash-lite` and newer Flash models).
- Groq models page: <https://console.groq.com/docs/models> (mentions `openai/gpt-oss-120b`, `llama-3.3-70b-versatile`, Qwen preview models).
- Free-tier rate limits for both: **UNKNOWN** — check each provider's rate-limit page.
- Safest check: list models with your own key (`GET https://api.groq.com/openai/v1/models`, and the Gemini `models.list` API).

---

## 8. Eval dataset format and how tests score

### Dataset — `src/email_assistant/eval/email_dataset.py`

Plain Python variables, not a data file:
- `email_input_1` … `email_input_16`: dicts with `author`, `to`, `subject`, `email_thread`.
- `triage_output_1` … `_16`: `"ignore" | "notify" | "respond"` (7 respond, 7 notify, 2 ignore).
- `response_criteria_1` … `_16`: bullet-point strings (`•`) for the judge.
- `expected_tool_calls`: list of lists of tool names (lower case), e.g. `["check_calendar_availability", "schedule_meeting", "write_email", "done"]`.
- `examples_triage`: LangSmith format `{"inputs": {"email_input": ...}, "outputs": {"classification": ...}}`.
- Parallel lists `email_inputs`, `email_names`, `response_criteria_list`, `triage_outputs_list`.

There is no split (dev vs held-out), no language field, no ID per example.

### Scoring

| Test | File | How it scores |
|---|---|---|
| `test_email_dataset_tool_calls` | `tests/test_response.py:252` | Only `respond` emails. Runs the graph, collects tool-call names from all messages (`utils.extract_tool_calls`). **Pass = every expected tool name appears.** Extra calls allowed. Order not checked. |
| `test_response_criteria_evaluation` | `tests/test_response.py:298` | Only `respond` emails. Formats all messages as text and asks `gpt-4o` (`CriteriaGrade{grade: bool, justification}`) with `RESPONSE_CRITERIA_SYSTEM_PROMPT`. **Pass = `grade` is True.** |
| `classification_evaluator` | `src/email_assistant/eval/evaluate_triage.py:54` | Exact string match of `classification_decision` vs label. Runs with `client.evaluate(...)` in LangSmith, `max_concurrency=2`, saves a bar chart PNG. Runs **on import** (it is a script). |
| `notebooks/test_tools.py` | — | Same tool-call check on 2 examples. |

`tests/run_all_tests.py` only tests `email_assistant` (no HITL). Its comment
(lines 48-52) says HITL graphs are skipped because the `Question` tool is not in the ground
truth, and a hard-coded `accept` resume would loop forever. **So the HITL + memory graph we
will build on has no automated tests today.** We need a test harness that answers
interrupts with scripted owner decisions.

Both pytest tests use `@pytest.mark.langsmith`, and `run_all_tests.py` passes
`--langsmith-output` and sets `LANGSMITH_TEST_SUITE`, so results are logged to LangSmith.

---

## 9. Which tests need which keys

| What | Command | Keys needed | Runs in the cloud with no keys? |
|---|---|---|---|
| Response tests | `python tests/run_all_tests.py --all` | `OPENAI_API_KEY` (agent gpt-4.1 + judge gpt-4o); `LANGSMITH_API_KEY` for logging | **No.** Fails at import (verified). |
| Notebook tests | `python tests/test_notebooks.py` | `OPENAI_API_KEY`, `LANGSMITH_API_KEY` (`evaluation.ipynb` creates LangSmith datasets) | **No.** |
| Triage eval | `python src/email_assistant/eval/evaluate_triage.py` | `OPENAI_API_KEY`, `LANGSMITH_API_KEY` | **No.** |
| `notebooks/test_tools.py` | `pytest notebooks/test_tools.py` | `OPENAI_API_KEY` | **No.** |
| Gmail graph / ingest | `run_ingest.py`, `setup_gmail.py` | Google OAuth token (`GMAIL_TOKEN` or `.secrets/token.json`) + OpenAI | **No.** |

Whether `@pytest.mark.langsmith` tests hard-fail or just skip logging when
`LANGSMITH_API_KEY` is missing: **UNKNOWN** (could not reach that point without an OpenAI key).

What **does** work with no keys today (verified by import):
`email_assistant.schemas`, `email_assistant.tools` (all 9 tools load), `email_assistant.utils`,
`email_assistant.eval.email_dataset`, `email_assistant.cron`.

**Bottom line: there are zero tests today that run without keys.** Phase 1a must create the first ones.

---

## 10. What CLAUDE.md, AGENTS.md and .github/workflows do

| File | What it does |
|---|---|
| `CLAUDE.md` | Overview of the 5 graphs and 5 notebooks, uv/pip setup, test commands. Says the package is installed as `interrupt_workshop` — but `pyproject.toml` names it `agents_from_scratch` (stale text). Import name `email_assistant` is correct. |
| `AGENTS.md` | Same content as `CLAUDE.md` minus the package-name line. Read by other coding agents (e.g. Codex). Does **not** get the academy rules unless we add them. |
| `.github/workflows/claude.yml` | On issue / PR comments or issues containing `@claude`, runs `anthropics/claude-code-action@beta` with `secrets.ANTHROPIC_API_KEY`. Permissions are read-only (`contents: read`), so it cannot push. |
| `.github/workflows/claude-code-review.yml` | On every PR opened/synchronised, runs the same action with a generic review prompt. Needs `ANTHROPIC_API_KEY`. |

- There is **no CI workflow that runs tests or lint.**
- Both workflows need an Anthropic API key (a paid key). Whether the secret is set in this repo: **UNKNOWN**. If it is not set, the review workflow will fail on every PR. Decide: remove it, or keep it and add the secret.
- Both pin `@beta` of the action; current recommended version: **UNKNOWN**.

---

## 11. Python version and key dependencies

| Item | Value | Source |
|---|---|---|
| `requires-python` | `>=3.11,<3.14` | `pyproject.toml` |
| `langgraph.json` python | `3.11` | `langgraph.json` |
| Cloud container | Python 3.13.18 | verified |
| **Your laptop** | **Python 3.14 — outside the allowed range.** `uv` can download and use a managed 3.13 for this project (`uv python install 3.13` / a `.python-version` file). | — |
| Package name / import | `agents_from_scratch` / `email_assistant` (from `src/email_assistant`) | `pyproject.toml` |

**`uv.lock` is stale.** It pins `langchain 0.3.25`, `langgraph 0.4.8`, `langsmith 0.3.44`,
while `pyproject.toml` asks for `langchain>=1.0.0`, `langgraph>=1.0.0`. Verified:
`uv lock --check` fails, and `uv sync` silently re-locks. After re-locking on 2026-11-03 it resolved to:

| Package | Resolved |
|---|---|
| langchain | 1.4.8 |
| langgraph | 1.2.19 |
| langsmith | 0.14.17 |
| pydantic | 2.11.5 |
| pytest | 8.4.0 |

(The re-lock was reverted; this audit changes no code. Phase 1a should commit a fresh lock.)

Other dependencies: `langchain-openai`, `langgraph-cli[inmem]`, `google-api-python-client`,
`google-auth-oauthlib`, `google-auth-httplib2`, `python-dotenv`, `pandas`, `matplotlib`,
`jupyter`, `pyppeteer`, `html2text`, `rich`. Dev: `mypy`, `ruff`.

Not present yet (needed later): `langchain-google-genai`, `langchain-groq`, `fastapi`,
`uvicorn`, `httpx` (or `supabase`), `psycopg` / `langchain-postgres` (pgvector), `mcp`,
`langgraph-checkpoint-postgres` or `-sqlite`.

Deprecation seen at runtime: `StateGraph(State, input=StateInput)` →
`LangGraphDeprecatedSinceV05: input is deprecated ... use input_schema` (removed in LangGraph 2.0).

---

## 12. Gaps against the academy hard rules

| Hard rule | Today's code | Gap |
|---|---|---|
| Owner approval on every send and calendar change | `write_email`, `schedule_meeting` are in `hitl_tools` | Enforced by a name list. A new write tool not added to the list would run **without** approval. Need "deny by default": only listed read-only tools skip approval. |
| No fee/date/seat count unless from a tool | Nothing | Needs a grounding check (code) + eval. |
| Sensitive never drafted | No such label; `notify` → owner can route to `response_agent` | Need a code-level block. |
| Anon key only, never write to EduChess | No EduChess code | Read-only client, GET only; test that it cannot write. |
| Minimal child data, synthetic only | Upstream `parse_gmail` prints full emails | Remove the prints; synthetic data in tests and evals. |
| No chess teaching | Nothing | Prompt rule + judge eval. |
| Keys in `.env`, never printed | `.env` is git-ignored; `.secrets/` ignored | Make sure logs never print keys or tokens. |
| Keep MIT licence and credit base repo | `LICENSE` present | Add credit in README (Phase 7). |
