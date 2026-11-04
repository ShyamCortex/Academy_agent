# Labelled parent messages

The test set for the Academy Support Agent. One message per line (JSONL, UTF-8, so Telugu text is fine).

| File | What it is |
|---|---|
| `sample_messages.jsonl` | 3 **SAMPLE** rows that show the format. Invented by the build assistant. Do not count them in your 40. |
| `parent_messages.jsonl` | **Your 40 real messages** (you create this file). |

All messages must be **synthetic**: made up, or rewritten so no real child or parent can be identified. Use `@example.invalid` addresses and first names only.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique text, e.g. `pm-001`. Sample rows use `SAMPLE-01`... |
| `split` | yes | `dev` (tune prompts on these) or `heldout` (look at once, at the end). Plan: 30 dev + 10 heldout. |
| `language` | yes | `en`, `te` (Telugu script) or `tenglish` (Telugu in English letters). Plan: at least 8 `te`/`tenglish`. |
| `text` | yes | The parent's message. |
| `expected_label` | yes | One of `book_demo`, `fees_plans`, `schedule`, `workshop_tournament`, `reschedule_cancel`, `sensitive`, `ignore`. |
| `expected_tool_calls` | yes | Tools a good run calls, lower case, from `find_demo_slots`, `get_plans`, `get_batch_timings`, `get_workshops`, `get_tournaments`, `create_demo_event`, `reschedule_demo`, `cancel_demo`, `send_reply`, `question`, `done`. Order is not checked. Must be `[]` for `sensitive` and `ignore`. |
| `must_include` | yes | Facts the reply must contain (list of short sentences, may be empty). Say where the fact comes from, e.g. "Fee exactly as returned by get_plans". |
| `must_not_say` | yes | Things the reply must not say (list, may be empty), e.g. "A fee that did not come from a tool", "Any chess advice". |
| `sender` | no | Display name and address. Default: `Parent <parent@example.invalid>`. |
| `sample` | no | `true` on the sample rows only. A finished set must have none. |

Unknown fields are rejected, so a typo in a field name is caught.

## Example (one line in the file)

```json
{"id": "pm-001", "split": "dev", "language": "en", "text": "Can my son try a demo class this week?", "expected_label": "book_demo", "expected_tool_calls": ["find_demo_slots", "send_reply"], "must_include": ["Offers only slots returned by find_demo_slots"], "must_not_say": ["Any fee that did not come from a tool"]}
```

## Check your file

```bash
uv run python -m email_assistant.academy.dataset data/eval/parent_messages.jsonl --full
```

Without `--full` it checks each row. With `--full` it also checks the finished-set rules: 40 messages, 30 dev + 10 heldout, at least 8 Telugu/Tenglish, no SAMPLE rows.
