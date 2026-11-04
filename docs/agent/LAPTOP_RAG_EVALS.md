# Laptop steps for Phase 3 (FAQ search) and Phase 5 (safety evals)

Everything here runs on **your laptop** (PowerShell, `uv`, repo folder). The cloud session cannot reach the model download
host, your keys, or your Supabase project. Do the steps in order.

Short word list:

- **embedding** — a list of 384 numbers that captures what a text means. Texts with similar meaning get similar lists.
- **pgvector** — a Postgres add-on that stores embeddings and finds the nearest ones.
- **recall@3** — of the 15 test questions, how many have a correct FAQ answer among the 3 results. 15 of 15 is 100%.
- **service key** — a Supabase key that can write. Only the loader uses it, only on your laptop, only for the **test** project.

## 0. Pull the branch and run the no-key tests

```bash
git fetch origin
git checkout claude/tender-johnson-kvwy6q
git pull
uv sync --extra dev
```

```bash
uv run pytest tests/unit -q
```

Expect `571 passed`. Then once more with the read-only tools going through the MCP server (same tests, same result):

```bash
$env:USE_MCP = "1"; uv run pytest tests/unit -q; Remove-Item Env:USE_MCP
```

`uv sync` installs `fastembed` and its ONNX runtime (no PyTorch, Windows wheels). The embedding model itself (about 70 MB) downloads the first time it is used.

## 1. Add the pgvector SQL (test Supabase project only)

1. Open the **test** project in Supabase (the one where you already ran `academy_test_schema.sql`). Not EduChess.
2. **SQL Editor → New query**. Paste the whole file `supabase/academy_faq_rag.sql`. Click **Run**.
   - Expect "Success. No rows returned".
   - `type "vector" does not exist`: the extension line did not run. Run `create extension if not exists vector with schema extensions;` alone, then the rest.
   - `relation "faq_chunks" already exists`: you ran it twice. Nothing to fix.
3. **Table Editor** → `faq_chunks` is there, empty, marked **RLS enabled**.
4. **Database → Functions** → `match_faq` is there.

## 2. Put the settings in `.env`

Copy the new lines from `.env.example`:

| `.env` name | Value |
|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` (already in `.env.example`) |
| `ACADEMY_FAQ_LOADER_KEY` | The **test** project's `service_role` / secret key: **Project Settings → API**. Used only by `scripts/ingest_faq.py`. |
| `FASTEMBED_CACHE_PATH` | Optional: `.cache/fastembed`, so the model is not re-downloaded if Windows cleans the temp folder. |

`ACADEMY_SUPABASE_URL` and `ACADEMY_SUPABASE_ANON_KEY` stay as before. The loader uses the same URL, so it can only write to the project the agent reads.
The service key is never given to the agent, to the tests or to the cloud. Do not paste it into a chat.

## 3. Check the embedding model before you load anything

First run downloads the model (about 70 MB, one time).

```bash
uv run python -m email_assistant.academy.rag_eval --index local
```

This embeds the FAQ in memory (no database) and prints recall@1 and recall@3 on the 15 questions. It saves `docs/agent/results/rag_recall_<date>.json`.
No bar is set in advance. Read the questions it missed. A miss is usually a FAQ wording problem or the Telugu-script gap (see Limits), not a bug.

## 4. Load the FAQ

Dry run first (embeds, shows the chunk ids, writes nothing, needs no service key):

```bash
uv run python scripts/ingest_faq.py --dry-run
```

Then the real load. It shows the project name it is about to write to and asks you to type it. If it is not your test project, stop.

```bash
uv run python scripts/ingest_faq.py
```

Expect `Done: 30 FAQ chunks are in 'faq_chunks'.` In **Table Editor → faq_chunks** you should see 30 rows (25 `en`, 5 `tenglish`).
Run it again any time you edit `data/faq/academy_faq.md`: rows are replaced by id and removed rows are deleted.

## 5. Check the real search path (anon key, through `match_faq`)

```bash
uv run python -m email_assistant.academy.rag_eval
```

This is the real path: the question is embedded locally, then `match_faq` is called in the test project with the anon key. The numbers should match step 3.
If it fails, see Troubleshooting.

Optional: the same search through the MCP server (starts it as a child process):

```bash
uv run python -m email_assistant.academy.mcp_bridge search_knowledge --args '{"question": "is the demo class free"}'
```

## 6. Write your 40 messages and check them

Create `data/eval/dev_messages.jsonl` (30 rows, `"split": "dev"`) and `data/eval/heldout_messages.jsonl` (10 rows, `"split": "heldout"`). Format: `data/eval/README.md`. Synthetic data only.

```bash
uv run python -m email_assistant.academy.dataset data/eval/dev_messages.jsonl data/eval/heldout_messages.jsonl --full
```

## 7. Run the dev evals

Settings needed in `.env`: `TRIAGE_MODEL`, `AGENT_MODEL`, `JUDGE_MODEL` (a different model from `AGENT_MODEL`), the Supabase URL and anon key, `EMBEDDING_MODEL`, `GROQ_API_KEY`.
No Google sign-in is needed: by default the eval uses a fake in-memory calendar (the same every run; a row's `existing_demo` booking is put in it first) and writes replies to `outbox/eval_outbox_<run-id>.jsonl`. Add `--real-calendar` only if you want real events in the Google test calendar.

A small trial on the sample rows first (costs a few calls):

```bash
uv run python -m email_assistant.academy.safety_eval --dataset data/eval/sample_messages.jsonl --limit 2
```

Then the dev set (30 messages, in batches of 5 with a 20-second pause):

```bash
uv run python -m email_assistant.academy.safety_eval
```

If a rate limit stops it, it says so and prints the exact command to continue. Run that command later (or tomorrow). Scored ids are skipped and the agent is not re-run for a reply that only needs the judge.
Slower pacing for a tight free tier: add `--batch-size 3 --pause 45`.

Results land in `docs/agent/results/`: `eval_dev-<date>.md` (the table), `.json`, `.jsonl`.

Then score the replies yourself so the judge can be compared with you:

1. Open `outbox/eval_review_dev-<date>.md` (local only). Read each reply **before** the judge's note.
2. Write `data/eval/my_scores.jsonl` (format in `data/eval/README.md`).
3. Rebuild the table with no model calls:

```bash
uv run python -m email_assistant.academy.safety_eval --report-only --run-id dev-<date>
```

Spot-check about 10 judge notes by hand. Do not tune prompts on the held-out set.

## 8. Run the held-out set once

Only when the dev numbers are final and you will not change prompts any more.

```bash
uv run python -m email_assistant.academy.safety_eval --heldout
```

It records the run in `docs/agent/results/heldout_lock.json`. A second run is refused unless you add `--force`, and then the set is no longer unseen. If a rate limit stops it part-way, run the same command again to continue the same run.
To read the held-out numbers again later without running anything:

```bash
uv run python -m email_assistant.academy.safety_eval --heldout --report-only
```

## 9. Commit the results

```bash
git add docs/agent/results data/eval/dev_messages.jsonl data/eval/heldout_messages.jsonl data/eval/my_scores.jsonl
git commit -m "docs: dev and held-out eval results"
git push
```

Do **not** add `outbox/`. It holds replies and is git-ignored.

## Pass bars (from the plan)

| Check | Bar |
|---|---|
| Sensitive recall | 100% |
| Sensitive never reaches the drafter | 100% |
| Drafts with every number in the tool outputs | 100% |
| Replies with no chess teaching (judge) | at least 95% |
| Triage accuracy, tool calls, reply quality, judge agreement, cost, latency | reported, no bar |

## Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| `Refused: This loader runs only on the owner's laptop` | A cloud, CI or pytest variable is set | Run it in a normal PowerShell window on your laptop |
| `Refused: ACADEMY_FAQ_LOADER_KEY is the public (anon) key` | Wrong key | Use the **service_role / secret** key of the test project |
| `... belongs to a different Supabase project than ACADEMY_SUPABASE_URL` | URL and key are from different projects | Use both from the same test project |
| loader: `answered HTTP 404 ... Did you run supabase/academy_faq_rag.sql?` | Step 1 not done, or the API schema cache is stale | Run step 1; wait a minute; or **Settings → API → Reload schema cache** |
| `rag_eval` / agent: `The data source answered with HTTP 404` | `match_faq` missing, or the cache is stale | Same as above |
| `... answered with HTTP 401` or `403` | Wrong anon key, or step 1's `grant` lines did not run | Re-copy the anon key; re-run the last lines of the SQL |
| `... answered with HTTP 400` | The database function got the vector in an unexpected form | Tell the build session. Send the exact line. Do not edit the SQL by hand. |
| `EmbeddingError: ... is not a model fastembed supports` | Typo in `EMBEDDING_MODEL` | Use the command printed in `.env.example` to list the 384-number models |
| `EmbeddingError: Could not load the embedding model` | The first download failed | Check the internet connection and run again |
| `The embedding model gives N numbers per text, but the database column holds 384` | Wrong model | Choose a 384-number model |
| `safety_eval`: `3 rate-limit failures in a row: stopping` | Free-tier limit (often the daily token limit) | Run the printed command later |
| `safety_eval`: `Refused: ... held-out message(s) ... run only with --heldout` | A held-out row is in your dev file | Move it to `heldout_messages.jsonl` |

## Limits you should know

- **Telugu script.** The default model is English-only. Tenglish (Telugu in English letters) works because it uses many English words ("fees", "demo", "batch"). A question written in Telugu script will probably retrieve poorly. Retrieval tuning for Telugu is under Later in `PLAN.md`.
- **The FAQ can go stale.** It states plan fees and event dates. The tool tells the model to prefer the live lookups (`get_plans`, ...) when they differ, and the FAQ does not quote seat counts.
- **The number check reads digits only.** "four classes" is not seen. A date or a time is matched on its own, not together with the other facts around it. See `grounding.py`.
- **The scripted owner accepts every draft.** The eval measures what the agent proposes. It does not measure what a real owner would edit.
