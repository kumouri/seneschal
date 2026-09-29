# Local semantic RAG index — setup (Advisor Chain, phase B)

The assistant's **Retrieval / Context advisor** (order 20) is a modular RAG pipeline: *query-transform →
retrieve → rerank + compress → augment* (`../references/advisor-chain.md`). **Phase A** retrieves from
baked-in references + Notion-search. **Phase B** (this) adds a **local semantic index** over the
assistant's own prose history — journal, notes, run-log — as one more retriever *behind the same
pipeline*, giving true semantic recall ("what did I decide about the database migration a month ago")
without leaning on keyword luck.

It's **free-but-local**: embeddings come from a local **Ollama** server, the index is stdlib **sqlite3**,
and everything is Python standard library — no `pip install`. It's also **additive**: if Ollama is down or
the index is empty, the retriever falls back to phase-A Notion-search. The semantic layer only ever *adds*.

## One-time setup

1. **Ollama.** Pull the embed model once:
   ```
   ollama pull nomic-embed-text
   ```
2. **Config is optional — but check the port.** Defaults (`localhost:11434`, `nomic-embed-text`,
   800-char chunks) are baked into `rag_common.py`. If your Ollama listens anywhere else, set
   `OLLAMA_URL`: a wrong port and a genuinely-absent Ollama produce the *same* exit 3 and the same
   "skip silently, it's a regenerable cache" policy, so a misconfigured index looks exactly like a
   healthy degraded one. To override, copy the example and edit:
   ```
   cp seneschal/scripts/rag.env.example seneschal/scripts/rag.env
   ```
   `rag.env` is gitignored.
3. **Build the initial index** from the local prose:
   ```
   python seneschal/scripts/rag_index.py --local
   ```
   The sqlite index lands at `seneschal/state/rag-index.sqlite` (gitignored, local-first).

## The two scripts

| Script | Role |
|--------|------|
| `rag_index.py` | Build/update the index. `--local` indexes run-log/carry-over; `--chat` indexes the owner<->assistant thread (`state/turns.jsonl`, see below); `--ingest F.jsonl` indexes records `{"source","ref","text"}` (how journal/notes get in). Incremental by doc hash; `--rebuild` starts clean; `--stats` reports (including the provenance refusal ledger). Every record passes the provenance guard first — see below. |
| `rag_query.py` | `rag_query.py "<query>" [--k N] [--source journal]` → top-k **current** chunks as JSON on stdout (see Bitemporal facts below), ranked by the dense + lexical hybrid. Exit **3** + `[]` when the embedder/index is unavailable (caller falls back). Every query also bumps the observe-only **salience access counters** (`--no-record` for analysis/debug reads) — see `SALIENCE_SETUP.md`. |

An index built by an older version upgrades in place on open — every schema change below is
additive — so there is never a rebuild or re-embed to run after updating.

## Bitemporal facts — "what is true now", not "what is similar"

Every doc carries a validity interval: `valid_from` (when the fact became true — ingest time unless
the record declares one), `valid_to` (NULL = still current), and an optional `supersedes` pointer.
An ingest record may declare both:

```json
{"source": "notes", "ref": "supplier-2026-07", "text": "New supplier is Acme.",
 "valid_from": "2026-07-29T00:00:00+00:00", "supersedes": "supplier-2026-03"}
```

`supersedes` names a `ref` in the **same source**; it stamps that older doc's `valid_to` with the new
record's `valid_from` — only while it's still open (a second superseder never overwrites the first
close). Records without these keys behave exactly as before.

Query behavior:

- **Default = current-only.** A doc with a real `valid_to` ≤ now is excluded and the next-best
  current memory backfills — a superseded fact returned by default is exactly the coherence bug this
  exists to kill (found three versions, used the stale one).
- **`--as-of <ISO ts>`** — time travel: returns what was current at that instant
  (`valid_from ≤ as-of < valid_to`-or-open; half-open, so at the instant of supersession the
  successor wins).
- **`--include-superseded`** — non-current rows come back too, each flagged `"current": false` with
  its `valid_to` and, when known, `superseded_by`.

**Nothing is ever deleted or hidden-forever** — superseded rows stay fully retrievable, flagged, the
same posture as salience's `disposable=1` (`../references/salience.md`). Currency and the
`disposable` ladder are independent axes.

## Hybrid retrieval — dense for content, lexical for names

Dense embeddings are bad at rare proper nouns: a bare name mentioned once can rank far down — below
the salience access floor — in a corpus that contains it. So an **FTS5** index (`chunks_fts`, compiled
into the SQLite that ships with Python — no new dependency) mirrors every chunk, and `rag_query.py`
fuses the two rankings by **reciprocal rank** (never by score; cosine and bm25 are incomparable). The
lexical arm only matches tokens **rare in this corpus** (under ~1% of chunks, with a small absolute
floor for young indexes) — matching common words makes answers worse — and it can only *reorder*
what the dense pass admitted, so it never re-admits a superseded fact. A hit it produced carries
`"lexical": true`, and `--no-lexical` turns it off. An index built before the arm existed gains the
table empty on open; the next non-dry-run `rag_index.py` backfills it in one statement.

## The chat corpus (`--chat`)

`rag_index.py --chat` reads `state/turns.jsonl` — the verbatim owner<->assistant thread written by
the chat channels — and indexes **one document per turn**, both speakers together (ref
`turn:<turn_id>`, `valid_from` = when it was said). Speakers are labelled with the configured names
from `persona/identity.json` (falling back to "Owner" / "Assistant"); system rows (job pushes,
reaction markers) are kept with a `(system)` label. **A `!private` turn is skipped entirely** — its
tombstone is honoured here too, whichever row of the turn carries it. Only the file is read, so the
flag is harmless (and indexes nothing) on an install whose chat channels don't write it yet.

## Provenance guard — what the index refuses

Everything retrieved from this index is injected into future turns with nobody in the loop, so
`rag_index.index_records` — the single write path every corpus goes through — runs
`provenance_guard.py` on each record **before its text is read**. The guard decides on the record's
`source` and a producer stamp, never on content, and **fails closed**: an unregistered source, a
stamp-required source without a stamp, or an unknown stamp is not persisted. Session distillations
must carry the `provenance` stamp `mini_dream.py` writes — `deterministic-fields` is archived,
`llm-excerpt` (a summary of verbatim transcript) is refused by design. Refusals print one line per
source/reason, land in the `provenance_refusals` table inside the index (refs and reasons, never
text), and show in `rag_index.py --stats`. The only override is `--allow-unattested SOURCE` on the
command line, and it is counted too. `python seneschal/scripts/provenance_guard.py --explain` prints
the registry. Why: `../references/comms-mapping.md`.

## How the corpus stays fresh — nightly in Dream

`rag_index.py` is deliberately **Notion-unaware** (pure, testable). The local prose it reads itself; the
Notion-resident corpus (journal, notes) is fetched by a run that *has* the Notion MCP — **Dream's nightly
refresh**:

1. Dream determines journal/notes entries new-or-changed since the last index.
2. It fetches their text via the Notion MCP and writes a JSONL
   (`{"source":"journal","ref":"<date/page>","text":"..."}` per line).
3. It runs `python seneschal/scripts/rag_index.py --local --chat --ingest <that.jsonl>` (act-low —
   local only). Session distillations ride in the same JSONL with their `provenance` stamp copied
   through verbatim (see the provenance guard above).
   A real (non-`--dry-run`) run stamps Dream step `2b` in `state/dream-steps.json`
   (`dream_steps.py`), so a night the index is skipped shows up as a stale ledger row, not silence.

Because indexing is incremental, unchanged docs are skipped, so the nightly pass is cheap. A full
`--rebuild --local` (plus a fresh ingest) rebuilds from scratch if the index is ever lost — it's a
regenerable cache, never a system of record.

**Embedding requests are sub-batched, and "the embedder is down" needs checking before you believe it.**
`rag_common.embed_texts` slices its input into requests bounded by **both** `EMBED_MAX_BATCH_ITEMS`
(64 items) and `EMBED_PAYLOAD_BUDGET_BYTES` (256 kB of encoded text), then concatenates the vectors.
`index_records` embeds one *document* per call, and a long-lived `run-log.md` is hundreds of chunks;
sent as one `/api/embed` body it comes back **HTTP 400**, which surfaces as `OllamaError` →
`rag_index.py` exit **3** → "skip silently, the embedder is unavailable" — an index that quietly stops
moving.

**The item cap is the one that matters.** That 400 is not a payload-size rejection; its body reads
`Post "http://127.0.0.1:PORT/tokenize": dial tcp ... actively refused it`. Ollama opens one internal
connection per input item, so a large batch overruns its own runner's listen backlog while the runner
stays alive. The failure is therefore **probabilistic and driven by item count** — single requests of
64 items pass reliably where a few hundred fail intermittently — and more, smaller requests are nearly
free, because the time is the embedding, not the round trips.

Debugging tips that follow from this: **read the error body, not just the status code** (a bare "400"
looks like a size limit and isn't), and if you see exit 3, check
`python seneschal/scripts/dream_steps.py status` — a stale **2b** row is the difference between a
genuinely-down Ollama and something failing inside a policy written to forgive one.

## The project-state layer (`rag_projects.py`)

The index also carries a **projects corpus** (source `project`): one state-summary doc per project the
owner is (or was) working on — where it lives on disk, branch, dirty state, recent commits, README
gist, and the matching GitHub repo — plus a doc for every GitHub repo with **no local clone**. That's
what lets the assistant answer *"what's the state of `<project>`?"* with either the state itself or
the exact place to go look.

```
cp seneschal/state/project-roots.example.json seneschal/state/project-roots.json   # then fill in real roots
python seneschal/scripts/rag_projects.py --ingest
```

- **Config** (`state/project-roots.json`, gitignored): `roots` are scanned recursively (bounded by
  `max_depth`, pruned below a found repo and inside `exclude_names`) for git repos; `non_git_roots`
  additionally get light summaries of their immediate unversioned project dirs; `extras` list stray
  projects anywhere on disk (e.g. a project embedded inside an app's install folder). `github.enabled` pulls
  the repo list via `gh` (already authed; additive — absent/failed `gh` just skips that layer).
- **Outputs:** `state/projects.jsonl` (the records), `state/projects-map.md` (a human-readable
  where-everything-lives table — the cheap orientation read), and — with `--ingest` — embedded docs in
  the index. All three are regenerable caches.
- **Freshness:** doc ids are `project:<path>`, indexing is incremental by hash (an unchanged project
  costs nothing), and after each ingest the `project` source is **reconciled** — docs for projects that
  vanished from the scan are dropped (`--no-prune` disables; the salience access ledger is never
  touched). **Dream re-runs `rag_projects.py --ingest` nightly** alongside the journal refresh, so the
  corpus tracks reality. Git reads run with `core.fsmonitor=false` (a filesystem-monitor tool can hang
  git in some repos) and per-command timeouts, so one sick repo degrades to a thinner summary, never a
  hung scan.

## How Retrieval uses it

Inside the Retrieval advisor's *retrieve* step, when the turn needs recall over the assistant's history,
it calls `rag_query.py` for semantic hits and blends them with the structured Notion/calendar reads, then
rerank+compress trims to a budget-bounded context. Exit 3 → treat as "no semantic layer this turn" and
proceed on Notion-search alone.
