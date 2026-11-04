# Results

Files written by the evals. Only ids, labels, tool names, yes/no flags, numbers and token counts are saved here.
Never message text and never a drafted reply (hard rule 5). Replies stay in the git-ignored `outbox/` folder.

| File | Written by | What it is |
|---|---|---|
| `triage_accuracy_<date>.json` | `email_assistant.academy.triage_accuracy` | Triage accuracy only |
| `rag_recall_<date>.json` | `email_assistant.academy.rag_eval` | recall@1 and recall@3 of the FAQ search on 15 questions |
| `eval_<run-id>.md` | `email_assistant.academy.safety_eval` | **The results table.** Start here. |
| `eval_<run-id>.json` | same | The same numbers as data |
| `eval_<run-id>.jsonl` | same | One row per message. The file a stopped run continues from. A message can appear on two lines (second line = after the judge); the later line wins. |
| `heldout_lock.json` | same, with `--heldout` | Records that the held-out run was made. A second run needs `--force`. Commit it. |

Run ids: `dev-<YYYYMMDD>` for a dev run, `heldout` for the held-out run.
