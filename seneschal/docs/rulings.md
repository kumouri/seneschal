# Rulings — the one place a dated decision lives

**Status:** `REFERENCE` — the design-decision ledger. It starts empty; rows are added as decisions are
made.

A decision about how the assistant must behave tends to live as narrative inside whichever spec or
grounding file needed it that day — and a decision that lives only as narrative gets re-asked, because
nothing points a later reader at it. This file is the durable, greppable record. A prompt-side file
states the **imperative** and points here by id; the **why** — the incident, the rejected alternative —
lives in the row, so trimming the prompt-side copy relocates the reasoning instead of deleting it.

**Format: Markdown, not JSONL** — this is a document whose whole job is to be read, and
`scripts/check_rulings.py` parses the table mechanically, so the format costs nothing on the
enforcement side.

**Row shape** — six columns, in this order:

| column | meaning |
|---|---|
| `id` | `YYYY-MM-DD-kebab-slug` — the date the decision was made, plus a short unique handle. Date-keyed rather than sequential, so two branches adding a row concurrently cannot collide on a counter. |
| `date` | `YYYY-MM-DD`, redundant with the id's prefix but kept so the table sorts and reads without parsing the id. |
| `rule` | **One imperative sentence** — the thing a run must do or refuse. Never the reasoning. |
| `enforced where` | `file:symbol` for a rule code enforces, or the literal string `prompt` for a rule only a turn honors. |
| `source` | Where the decision was made — a spec's `§` heading, or the grounding file's own section. |
| `incident` | One line: what happened that makes the rule non-arbitrary. |

**How a row gets here.** `check_rulings.py --enforce` (a CI gate) blocks a change that adds
"ruling"/"ruled" language to a tracked `.md` file without also touching this file. It does not check
that a row is *true* — only that a decision newly narrated somewhere left a row here. When the word is
incidental (the idiom "ruled out", say), mark that line with an HTML comment instead:
`<!-- check-rulings: not-a-ruling — <reason> -->` — the reason is mandatory.

## The ledger

| id | date | rule | enforced where | source | incident |
|---|---|---|---|---|---|
