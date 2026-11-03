# Plan: EduChess Academy Support Agent

Built on the audit in [`AUDIT.md`](AUDIT.md). Hard rules live in [`CLAUDE.md`](../../CLAUDE.md#academy-support-agent-rules).

## Status

| Phase | Name | Branch | Hours (est.) | Status |
|---|---|---|---|---|
| 0 | Audit + plan | `docs/audit` | 2 | Done, waiting for go |
| 1a | Run on Gemini + Groq via env vars | `phase/1a-models` | 4 | Not started |
| 1 | Retarget to EduChess (labels, prompts, guards, data format) | `phase/1-retarget` | 8 | Not started |
| 2 | Real tools (Google Calendar, EduChess read-only) | `phase/2-real-tools` | 10 | Not started |
| 3 | RAG over academy FAQ (pgvector) | `phase/3-rag` | 7 | Not started |
| 4 | MCP server for academy tools | `phase/4-mcp` | 4 | Not started |
| 5 | Evals (triage, safety, quality) | `phase/5-evals` | 9 | Not started |
| 6 | Ship (FastAPI service, deploy) | `phase/6-ship` | 9 | Not started |
| 7 | Show (README, diagram, demo video) | `phase/7-show` | 6 | Not started |
| | **Total** | | **~59** | |

Hours include time for you to read and understand the change.

## How every phase runs

```
new branch from main
  → run no-key tests (baseline)
  → smallest change that meets "Done when"
  → run no-key tests again
  → commit, push, update this table
  → you: pull on laptop, run real-model tests
  → teach-back: summary, 3 key files, 1 trade-off, 3 quiz questions, 1 hand change
  → you merge, then say "go" for the next phase
```

Where work happens:

| Kind of work | Cloud session | Your laptop |
|---|---|---|
| Write code, prompts, fakes | ✅ | |
| No-key tests (fake models, fake calendar, recorded fixtures) | ✅ | ✅ |
| Real Gemini / Groq calls, LangSmith runs | | ✅ |
| Google OAuth browser login, real calendar | | ✅ |
| Supabase anon-key reads of EduChess | | ✅ |
| Writing the 40 labelled messages | | ✅ (you) |
| Dockerfile, `docker build`, deploy, demo video | | ✅ (you) |

Laptop one-time setup (Windows, PowerShell). Your Python is 3.14 but the project allows `<3.14`, so let uv fetch 3.13 for this project only:

```bash
uv python install 3.13
```

```bash
uv sync --extra dev --python 3.13
```

---

## Phase 1a — Run on Gemini + Groq through env vars (smallest change)

**Goal:** the existing graphs run on Gemini (triage + agent) with a Groq judge, chosen only by env vars, with retry on rate limits. No academy changes yet.

**Done when:**
- No `init_chat_model("openai:...")` left in `src/` or `tests/` (notebooks untouched).
- `.env.example` lists `GOOGLE_API_KEY`, `GROQ_API_KEY`, `TRIAGE_MODEL`, `AGENT_MODEL`, `JUDGE_MODEL`, retry settings, `LANGSMITH_*`. No model id hard-coded in Python.
- A missing model env var gives a clear error naming the variable.
- Cloud: a new no-key test suite (`tests/unit/`) passes with a fake chat model: every graph module imports, and `email_assistant` runs end to end on one email.
- Laptop: `python tests/run_all_tests.py --all` runs on Gemini with the Groq judge (record the pass rate; 100% not required).

**Steps:**
1. Add `langchain-google-genai`, `langchain-groq` to `pyproject.toml`; re-lock `uv.lock` (it is stale today); add `.python-version` = `3.13`.
2. Check the current model ids and the API-key env var names in the official Gemini and Groq docs; put them only in `.env.example`.
3. New `src/email_assistant/models.py`: `get_model(role)` → reads `TRIAGE_MODEL` / `AGENT_MODEL` / `JUDGE_MODEL`, calls `init_chat_model(value, temperature=0)`, wraps with retry + exponential backoff + jitter on rate-limit errors. A test hook lets tests swap in a fake.
4. Replace the 12 hard-coded calls (list in AUDIT §7). Memory update uses `AGENT_MODEL`.
5. Fake chat model in `tests/fakes.py` that supports `bind_tools` and `with_structured_output` and returns scripted outputs.
6. Fix `input=` → `input_schema=` deprecation only if it blocks; otherwise leave.

**Files touched:** `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `src/email_assistant/models.py` (new), 5 graph files, `tests/test_response.py`, `tests/fakes.py` + `tests/unit/` (new).

**Risks:**
- Gemini may not accept `tool_choice="required"` or `"any"` the same way → test on laptop first; fall back to the value the provider supports.
- Pydantic-class tools (`Done`, `Question`) may be declared differently by Gemini.
- Free-tier rate limits make the 2×N test run slow → run with `-n 1`, retry with backoff.
- Groq structured output depends on the model → choose a judge that supports tool calling or JSON schema.

**Prompt to paste:**
```
Phase 1a. Branch phase/1a-models from main. Follow docs/agent/PLAN.md "Phase 1a" and the
CLAUDE.md academy rules. Run the no-key tests before and after (create them if none exist).
Look up the current Gemini Flash-Lite, Gemini Flash and Groq model ids and API-key env var
names in the official docs; put them only in .env.example. Add get_model(role) with retry and
exponential backoff, replace every hard-coded init_chat_model in src/ and tests/, add a fake
chat model for no-key tests. Commit, push, update PLAN.md status, give me the laptop commands
and the teach-back.
```

---

## Phase 1 — Retarget to EduChess

**Goal:** a new academy graph with the 7 parent labels, EduChess prompts, code-level safety guards, and the file format for your 40 labelled messages. Still mock tools.

**Design:**
- New package `src/email_assistant/academy/`: `graph.py` (copied from `email_assistant_hitl_memory.py`), `prompts.py`, `schemas.py`, `tools.py` (mocks), `guards.py`.
- Labels: `book_demo`, `fees_plans`, `schedule`, `workshop_tournament`, `reschedule_cancel`, `sensitive`, `ignore`.

| Label | Route |
|---|---|
| `book_demo`, `fees_plans`, `schedule`, `workshop_tournament`, `reschedule_cancel` | `response_agent` (draft → owner approval) |
| `sensitive` | owner notification only. **No "respond" button, never reaches `response_agent`.** Guard checked in code. |
| `ignore` | END |

- Guards in code (not prompts, not memory):
  - Deny by default: only tools on a read-only allow-list run without approval; every other tool interrupts.
  - `sensitive` can never reach `response_agent`, whatever memory or owner says.
  - Memory updates cannot change the label list or the sensitive rule (they only edit free-text preferences).
- Today's date and time computed per call in `Asia/Kolkata` (fix the import-time `datetime.now()`).
- Remove the full-email `print` for academy code paths.
- Dataset format: `data/eval/parent_messages.jsonl`, one JSON object per line:

```json
{"id": "pm-001", "split": "dev", "lang": "en", "label": "book_demo",
 "from": "Parent A <parent.a@example.com>", "subject": "Demo class for my son",
 "body": "…", "expected_tools": ["find_demo_slots", "create_demo_event", "send_reply"],
 "criteria": ["Offers only slots returned by find_demo_slots", "No fee amounts"]}
```

  - `split`: `dev` (30) or `heldout` (10). `lang`: `en`, `te`, `tenglish`.
  - A loader + validator (`src/email_assistant/academy/dataset.py`) checks fields, labels, unique ids, 30/10 split, 8 Telugu/Tenglish. Ships with **2 obviously-fake template rows only**; you write the 40.
- Add a no-key CI workflow (`.github/workflows/tests.yml`) that runs `tests/unit/`.

**Done when:**
- No-key tests prove: each label routes as in the table; a `sensitive` message never produces a draft; an unlisted write tool still interrupts; dataset validator accepts the template and rejects bad rows.
- Graph registered in `langgraph.json` as `academy_agent`.
- Laptop: `langgraph dev` shows the academy graph; 3 hand-written messages triage sensibly on Gemini Flash-Lite.

**Files touched:** `src/email_assistant/academy/*` (new), `langgraph.json`, `data/eval/parent_messages.jsonl` (template), `tests/unit/test_academy_*.py`, `.github/workflows/tests.yml`.

**Risks:**
- Flash-Lite may confuse `schedule` vs `reschedule_cancel` → clear label definitions + few-shot examples in the prompt.
- Telugu/Tenglish triage quality is unknown until your data exists.
- Memory drift → limited by the code guards above.

**Prompt to paste:**
```
Phase 1. Branch phase/1-retarget from main (after 1a is merged). Follow docs/agent/PLAN.md
"Phase 1" and the CLAUDE.md academy rules. Build src/email_assistant/academy/ from
email_assistant_hitl_memory.py with the 7 labels, EduChess prompts, code guards (deny-by-default
approval, sensitive never drafted), Asia/Kolkata dates per call, and the JSONL dataset format +
validator with 2 fake template rows only. Add a no-key CI workflow. No-key tests before and
after. Commit, push, update PLAN.md, give me laptop commands and the teach-back.
```

---

## Phase 2 — Real tools

**Goal:** real Google Calendar (find slots, create event) and EduChess read tools using only the Supabase anon key.

**Tools:**

| Tool | Kind | Approval | Backend |
|---|---|---|---|
| `find_demo_slots(date_from, date_to)` | read | no | Google Calendar `freebusy` + academy hours rules, IST |
| `create_demo_event(parent_email, child_first_name, start, end)` | **write** | **yes** | Calendar `events.insert`, `Asia/Kolkata` |
| `get_batches()`, `get_fee_plans()`, `get_upcoming_events()` | read | no | Supabase REST (PostgREST) **GET only**, anon key |
| `send_reply(thread_id, body)` | **write** | **yes** | Gmail reply (reuse upstream `send_email`) |

- EduChess client: only `GET`; a test asserts no POST/PATCH/PUT/DELETE is possible; URL + anon key from `.env`.
- Each tool returns structured data with a `source` field, so Phase 5 can check every number came from a tool.
- Child data: first name only; no age/DOB unless the parent wrote it.
- Cloud tests use a fake calendar and recorded Supabase JSON fixtures (synthetic).

**Done when:**
- No-key tests: slot finder respects busy times and academy hours; `create_demo_event` always interrupts; EduChess client cannot write.
- Laptop: find slots on a **test calendar**, owner approves, event appears; one EduChess read returns real rows.

**Files touched:** `src/email_assistant/academy/tools/{calendar.py,educhess.py,gmail.py}`, `.env.example`, `tests/unit/test_tools_*.py`, `tests/fixtures/`.

**Risks / unknowns:**
- EduChess table and column names: **UNKNOWN** — you must share the schema (names only).
- Supabase Row Level Security must allow anon `select` on those tables: **UNKNOWN**.
- Google OAuth consent screen in "testing" mode expires refresh tokens (UNKNOWN for your project) → re-auth on laptop.
- Use a separate test calendar, never the academy's live one, while developing.

**Prompt to paste:**
```
Phase 2. Branch phase/2-real-tools from main. Follow docs/agent/PLAN.md "Phase 2" and the
CLAUDE.md academy rules. Here is the EduChess schema (table/column names only): <paste>.
Build find_demo_slots (freebusy, IST, academy hours), create_demo_event (always approval),
read-only EduChess tools over Supabase REST with the anon key (GET only, tested), and wire
send_reply through approval. Fake calendar + synthetic fixtures for no-key tests.
Commit, push, update PLAN.md, laptop commands, teach-back.
```

---

## Phase 3 — RAG over the academy FAQ

- RAG (retrieval-augmented generation) — fetch the most relevant FAQ passages and give them to the model, so it answers from your text, not from memory.
- pgvector — a Postgres extension that stores embeddings (number vectors) and finds the nearest ones.

**Goal:** `search_faq(question)` returns the top FAQ chunks with ids; the drafter must use them for policy answers.

**Steps:** FAQ markdown in `data/faq/` (you write it) → chunk → embed (Gemini embedding model, id from env) → store in pgvector → `search_faq` tool (read, no approval).

**Open decision (anon-key rule):** the anon key cannot write to EduChess. Options:

| Option | Write path | Read path | Note |
|---|---|---|---|
| A. Separate free Supabase project owned by the agent | service key, ingest script on laptop only | anon key | Cleanest; recommended |
| B. EduChess Supabase, you load rows by hand in the dashboard | you, outside the agent | anon key + `match_faq` RPC | No agent write path at all |
| C. Local Postgres + pgvector in Docker | local | local | Not deployable as-is |

**Done when:** no-key tests with a fake embedder + in-memory index pass; laptop: 5 FAQ questions return the right chunk in top 3.

**Files touched:** `src/email_assistant/academy/rag/{ingest.py,search.py}`, `data/faq/*.md`, SQL migration file, tests.

**Risks:** embedding model free-tier limits; Telugu queries vs English FAQ (cross-lingual retrieval quality UNKNOWN).

**Prompt to paste:**
```
Phase 3. Branch phase/3-rag from main. Follow docs/agent/PLAN.md "Phase 3", option <A/B/C>.
Build ingest (laptop-only script) and search_faq (read-only tool) over pgvector, embedding
model id from env, fake embedder for no-key tests. Commit, push, update PLAN.md, laptop
commands, teach-back.
```

---

## Phase 4 — MCP server for academy tools

- MCP (Model Context Protocol) — a standard way to expose tools so any MCP client (Claude Desktop, other agents) can call them.

**Goal:** `src/email_assistant/academy/mcp_server.py` (official `mcp` Python SDK) exposing **read-only** tools: `find_demo_slots`, `get_batches`, `get_fee_plans`, `get_upcoming_events`, `search_faq`.

**Decision:** write tools (`create_demo_event`, `send_reply`) are **not** exposed over MCP, because MCP clients would bypass the graph's owner approval.

**Done when:** an in-process MCP client test lists exactly the read-only tools and calls one with fixtures (no keys). Laptop: connect from an MCP client (e.g. MCP Inspector) and call `get_batches`.

**Files touched:** `academy/mcp_server.py`, `pyproject.toml` (`mcp`), tests, short README section.

**Risks:** SDK API changes between versions → pin the version.

**Prompt to paste:**
```
Phase 4. Branch phase/4-mcp from main. Follow docs/agent/PLAN.md "Phase 4". Expose only the
read-only academy tools via the official MCP Python SDK, reuse the Phase 2/3 functions, test with
an in-process client and fixtures. Commit, push, update PLAN.md, laptop commands, teach-back.
```

---

## Phase 5 — Evals

**Goal:** numbers you can put on a resume, measured on your 40 messages.

| Eval | Type | Pass bar |
|---|---|---|
| Triage accuracy + confusion matrix (dev 30, held-out 10) | exact match, code | report; held-out run only at the end |
| Sensitive never drafted | code check on graph trace | **100%** |
| No invented numbers (₹, dates, times, seat counts) | code: every number in the draft must appear in that run's tool outputs | **100%** |
| No chess teaching | Groq judge | ≥ 95% |
| Reply quality vs your `criteria` | Groq judge (different family from Gemini) | report |
| Expected tools called | code (like upstream) | report |

- Runs in LangSmith as experiments; local JSON + markdown summary in `docs/agent/results/`.
- Scripted owner decisions resolve interrupts (accept / edit / ignore), so HITL graphs are testable (upstream skips them).
- Concurrency 1 + retry for free tiers.

**Done when:** no-key tests verify each scorer on hand-made traces; laptop: full dev run done, results committed.

**Files touched:** `src/email_assistant/academy/eval/*`, `tests/eval/*`, `docs/agent/results/`.

**Risks:** judge bias / disagreement → spot-check 10 judge results by hand; held-out leakage → never tune prompts on held-out.

**Prompt to paste:**
```
Phase 5. Branch phase/5-evals from main. Follow docs/agent/PLAN.md "Phase 5". Build triage,
sensitive-never-drafted and number-grounding scorers in code, Groq-judge scorers for chess
teaching and quality, a scripted-owner interrupt harness, LangSmith experiment runner with
concurrency 1. Unit-test every scorer with no keys. Commit, push, update PLAN.md, laptop
commands, teach-back.
```

---

## Phase 6 — Ship

**Goal:** a FastAPI service and one cloud deploy.

**API sketch:**

| Method + path | Purpose |
|---|---|
| `POST /messages` | ingest a parent message → runs until first interrupt or END |
| `GET /pending` | owner's queue of drafts / bookings / sensitive notices |
| `POST /pending/{thread_id}/decision` | accept / edit / ignore / respond → resumes the graph |
| `GET /healthz` | health check |

- Auth: one owner API key header (from env).
- Persistent checkpointer + store: **decision needed** (Postgres in the agent's own DB from Phase 3 option A, or SQLite on a volume).
- **You write the Dockerfile** (rule). I only list what it needs: Python 3.13, `uv sync --frozen`, `uvicorn` command, port, no secrets baked in.
- Deploy target: **UNKNOWN / your choice** (e.g. Google Cloud Run, Render, Fly.io); secrets via the platform's secret store.

**Done when:** no-key API tests with `TestClient` + fake models pass (ingest → pending → approve → tool runs once); laptop: container runs locally; deployed URL answers `/healthz`.

**Files touched:** `src/email_assistant/academy/api.py`, `pyproject.toml` (`fastapi`, `uvicorn`, checkpointer package), tests. **Not** `Dockerfile`.

**Risks:** free-tier cold starts; checkpointer DB limits; never log message bodies.

**Prompt to paste:**
```
Phase 6. Branch phase/6-ship from main. Follow docs/agent/PLAN.md "Phase 6", checkpointer
<choice>, deploy target <choice>. Build the FastAPI service with owner auth and approval
endpoints, TestClient tests with fakes. Do not create or edit the Dockerfile; tell me what it
needs. Commit, push, update PLAN.md, laptop + deploy commands, teach-back.
```

---

## Phase 7 — Show

**Goal:** a reviewer understands the project in 2 minutes.

- README rewrite: what it does, architecture diagram (mermaid), hard rules and how code enforces them, eval table, how to run, **credit to `langchain-ai/agents-from-scratch` + MIT licence kept**.
- Demo video script (3 min): triage of 3 messages incl. one sensitive and one Tenglish → booking with approval → eval numbers.
- Resume bullets drafted from real eval numbers only.

**Done when:** README renders on GitHub; you record the video.

**Prompt to paste:**
```
Phase 7. Branch phase/7-show from main. Follow docs/agent/PLAN.md "Phase 7". Rewrite README
with credit to agents-from-scratch and the MIT licence, add a mermaid architecture diagram,
fill the eval table from docs/agent/results, write a 3-minute demo script and resume bullets
using only measured numbers. Commit, push, update PLAN.md, teach-back.
```

---

## Open questions for you

1. Which deploy target (Phase 6)?
2. RAG storage option A, B or C (Phase 3)?
3. EduChess table/column names and RLS status (Phase 2).
4. Keep or remove the two Claude GitHub workflows (they need a paid Anthropic key)?
5. Should `AGENTS.md` also get the academy rules (for non-Claude coding agents)?
