# Laptop setup for Phase 2 and Phase 4

Everything here runs on **your laptop**. The cloud session cannot see your `.env` or open a browser.
Commands are for PowerShell in the repo folder, with `uv`.

Short word list (used below):

- **anon key** — the public key of a Supabase project. It can only do what the database rules allow. Here: read four tables.
- **RLS (row-level security)** — database rules that decide which rows each role can read or change.
- **OAuth** — the "Sign in with Google" flow. You log in once in a browser. Google then gives the program a token.
- **token** — a saved proof that you signed in. Treat it like a password. It lives in a git-ignored file.
- **MCP (Model Context Protocol)** — a standard way to offer tools, so any MCP client can call them.

## 0. Pull the branch and run the no-key tests

```bash
git fetch origin
git checkout claude/kind-bell-u63k9h
git pull
uv sync --extra dev
```

```bash
uv run pytest tests/unit -q
```

Expect `324 passed`. Run it once more with MCP on (the read-only tools then go through the MCP server; the same tests must pass):

```bash
$env:USE_MCP = "1"; uv run pytest tests/unit -q; Remove-Item Env:USE_MCP
```

## A. Supabase: the separate test project

1. Go to supabase.com and create a **new project** named `academy-agent-test`. Use the free plan. Do **not** use the EduChess project or organisation.
2. Open **SQL Editor → New query**. Paste the whole file `supabase/academy_test_schema.sql`. Click **Run**.
   - Expect "Success. No rows returned".
   - If you see `relation "plans" already exists`, stop. You are in the wrong project, or you ran it twice.
3. Open **Table Editor**. You should see 4 tables (`plans`, `batches`, `workshops`, `tournaments`), each marked **RLS enabled**. Row counts: 2, 2, 1, 1.
4. Open **Project Settings → API** (or **API Keys**). Copy **two values** into `.env`:

   | `.env` name | Where to find it |
   |---|---|
   | `ACADEMY_SUPABASE_URL` | "Project URL", like `https://abcd1234.supabase.co` |
   | `ACADEMY_SUPABASE_ANON_KEY` | the **anon / public** key (legacy tab) or the **publishable** key (`sb_publishable_...`) |

   Never copy the `service_role` or **secret** key. The code refuses any key that is not an anon or publishable key.
5. Check it. No model and no Google needed:

   ```bash
   uv run python -m email_assistant.academy.data_tools
   ```

   Expect four lines of JSON with `"source": "supabase test project: ..."` and your made-up rows.

Notes:
- To change the made-up facts, edit `src/email_assistant/academy/config.py`, run `uv run python -m email_assistant.academy.seed_sql --write`, and run the new SQL in a fresh test project. A test fails if the SQL and `config.py` disagree.
- Supabase **pauses a free project after 7 days without use**. If a tool says the data source answered with an error, open the dashboard and click **Restore**.

## B. Google Cloud: a test account and a test calendar

Use a **test Google account**, not your personal or work one.

1. Sign in to console.cloud.google.com with the test account. Create a project named `academy-agent-test`.
2. **APIs & Services → Library**. Search **Google Calendar API**. Click **Enable**.
3. **OAuth consent screen** (now called **Google Auth Platform**):
   - Audience / user type: **External**. Publishing status: **Testing**.
   - **Test users**: add the test account's email.
   - **Data access / Scopes**: add `https://www.googleapis.com/auth/calendar.events`. This is the only permission the code asks for.
4. **Credentials (Clients) → Create client → Application type: Desktop app**. Click **Download JSON**.
5. Save that file as **`.secrets/academy_google_credentials.json`** in the repo folder. Make the `.secrets` folder if it does not exist. The folder is git-ignored.
6. Make the test calendar. In Google Calendar (test account): **Other calendars → + → Create new calendar**, name it `Academy Test`.
   Open its **Settings → Integrate calendar** and copy the **Calendar ID** (it ends in `@group.calendar.google.com`).
7. Put it in `.env`:

   ```
   GOOGLE_CALENDAR_ID=<the calendar id>
   ```

   There is no default on purpose. The code never falls back to a personal "primary" calendar.

Google's "Testing" mode ends your sign-in after **7 days**. When tools say "The Google sign-in has expired", run step C again.

## C. First OAuth sign-in

```bash
uv run python -m email_assistant.academy.google_auth
```

1. A browser opens. Choose the **test account**.
2. Google says "Google hasn't verified this app". That is normal in Testing mode. Click **Advanced → Go to ... (unsafe)**, then **Allow**.
3. The terminal prints `Signed in. Saved the sign-in to .secrets/academy_google_token.json.` then `OK: the saved sign-in can read the test calendar.`

To re-check later without a browser:

```bash
uv run python -m email_assistant.academy.google_auth --check
```

## D. One end-to-end run

You need in `.env`: the model settings from Phase 1a (`TRIAGE_MODEL`, `AGENT_MODEL`, `GROQ_API_KEY`), the two Supabase values, and `GOOGLE_CALENDAR_ID`.

**Terminal 1** — start the graph server:

```bash
uv run langgraph dev
```

**Terminal 2** — send the booking sample `SAMPLE-04` and wait:

```bash
uv run python -m email_assistant.academy.e2e
```

The command prints the thread id and then waits. Do this in **LangGraph Studio** (use the Studio link that `langgraph dev` printed):

1. Pick the graph `academy_agent` and open the thread with the printed id.
2. The run stops at the first request: `create_demo_event`. Read the date and the child's name.
3. Resume with `[{"type": "accept"}]`.
4. It stops again at `send_reply`. Read the reply. Resume with `[{"type": "accept"}]`.

The terminal then prints two checks:

```
PASS  reply in the outbox (outbox\academy_outbox.jsonl): 1 new reply to the parent
PASS  demo event in the test calendar: 1 found, start 2026-...
```

Look at the result yourself too: the event is in the `Academy Test` calendar, and `outbox/academy_outbox.jsonl` holds the reply (git-ignored; synthetic data only).

To decline instead, resume with `[{"type": "ignore"}]`. Then nothing is written. The checks print `FAIL`, which is correct.

## E. Phase 4: the MCP server

```bash
uv run python -m email_assistant.academy.mcp_bridge get_plans
```

It starts the MCP server as a child process, lists its tools (exactly six since Phase 3, all read-only), and calls `get_plans`.
To make the agent use MCP for its read-only tools, put `USE_MCP=1` in `.env` and restart `langgraph dev`.
Write tools never go over MCP.

## F. The `model_check --list` fix

```bash
uv run python -m email_assistant.model_check --list
```

The Google list should now print model ids instead of "client has been closed".

## What was and was not checked in the cloud

| Checked in the cloud | Needs your laptop |
|---|---|
| All no-key tests (324), with MCP off and on | Real Google Calendar calls (OAuth, the real `events` API) |
| Google's own client library builds the exact requests (recorded, not sent) | Real Supabase queries (the filter syntax `event_date=gte.`, `order=`) |
| `e2e` against a real `langgraph dev` server with fake models | The real Groq model choosing the right tools and arguments |
| The real MCP server as a child process | The Studio screen itself (the resume box) |
| `model_check` Google listing against a local stub of the Gemini API | The real Gemini/Groq listings |
