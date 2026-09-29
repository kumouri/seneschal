# Dream mode — nightly consolidate (the wind-down)

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — Dream is its
own leaf so it stops paying for Chat's rules; each step's *rationale* sits in the leaf that owns it.

**Read this whole file before acting, then work its steps in order.**

**First, run `python scripts/dream_steps.py status`** and treat any OVERDUE row as tonight's first
priority. **There is no such thing as a "scoped Dream"** — it is defined nowhere, and improvising one
is how a step goes unrun for weeks while each night's skip is recorded only in prose. **If you must skip
a step, record it:** `python scripts/dream_steps.py record <step> --skip --reason "<why>"`. Steps with an
owning script stamp themselves and **cannot** be cleared by hand — only doing the work clears the alarm.
Why: `scripts/dream_steps.py`.

---

**Advisors:** full chain (incl. **Oikonomos**) **+ a `Propose-Learnings` advisor** (order 45, `out:`
step) — spot patterns, draft gated proposals, open the PR and merge it on green; never auto-apply.

The assistant's "sleep → dream → wake": a nightly run (after Wrap) that condenses the day, sets up
tomorrow, and proposes what it could learn — each run ends by writing to the durable stores the next run
(and the morning Brief) reads, which is how the local-first design organizes its thoughts without a
fragile always-on session. **`state/context-digest.md` is retired — Dream no longer writes it.** The
three jobs it used to do each have their own home now: the day's summary is the Run Log itself (step 4
— nothing extra to write here), open loops live in the open-work register (`state/open-loops.json` via
`scripts/loops.py`, rendered into `state/carry-over.md`'s generated region), and pending reminders live
in `state/reminders.json` (reconciled in step 2). What follows is the one job that never lived in the
digest's prose — the standing-safety contract — plus the migration this retirement still owes.

On a Dream run:

1. **Standing-safety maintenance (act-low, local) → `state/standing-safety.json` via
   `scripts/standing_safety.py`.** The standing-safety block (*read first — standing safety items, do
   NOT re-raise cold*) is a **CONTRACT, not a habit**: `presence.py` injects that store's rendered
   content verbatim into every warm session's cold grounding — it is the only part of the assistant's
   durable memory that reaches it without a tool read. Touch the store ONLY through this CLI — never by
   hand-editing the JSON, and never by writing prose that describes an item without also running the
   command:
   - **One-time migration — ONLY while the store is still empty.** `python scripts/standing_safety.py
     list` shows nothing AND `state/context-digest.md` still exists on this host (a leftover from before
     retirement) → run `python scripts/standing_safety.py import-digest` (no `--force`) exactly once,
     folding its read-first bullets in as `retire_when: standing` rows for later reclassification. **The
     CLI itself refuses a second run without `--force`** — once the store holds anything at all, this
     bullet is a no-op forever, by construction, not by discipline.
   - **A new standing-safety item** (something just decided, told to the owner, or closed tonight) —
     `python scripts/standing_safety.py add "<the instruction, verbatim>" --source <a decision reference,
     a memory file, or "tonight's Dream run"> --retire-when <a date | "when <decision-id> lands" |
     standing>`. A `retire-when` that is none of those three shapes is refused, not stored — never guess
     one; `standing` is always a safe default for something with no known end.
   - **Retiring what has expired** — first `python scripts/standing_safety.py list --due` (only
     DATE-shaped items whose date has passed; a `"when <decision> lands"` item never shows here because
     the script cannot verify that on its own). For each id it prints, `python
     scripts/standing_safety.py retire <id> --reason "<why it no longer needs saying>"`.
   - **Decision-gated items** — `python scripts/standing_safety.py list` and, for any item whose
     `retire_when` reads `"when <decision-id> lands"`, check whether that decision has now landed; if it
     has, retire it the same way.
1b. **Pre-stage the Brief's Phase-1 store inputs (act-low, read-only) → `state/brief-prestage.json` via
   `scripts/brief_prestage.py`.** In the same pass, snapshot the Brief's read-only store reads so the
   morning Brief can skip its full 4-read batch and only delta-check (`store/notion/mapping.md` →
   Throughput). `store-query`, in **one parallel batch**: open **Tasks** due/overdue **today _and_
   tomorrow** (status not in (done, archived) and due ≤ tomorrow), **Active/Carrying-Over Important
   Flags**, and **In-Progress Projects** (see `references/databases.md`; the Notion backend's exact
   option strings + `date:Due:start` filter are in `store/notion/mapping.md`). Assemble `{"tasks":
   [...], "flags": [...], "projects": [...]}` — each item carrying its name + `id`/`url` so the Brief
   can cite and diff — and pipe it in: `python scripts/brief_prestage.py write` (stdin JSON; it stamps
   `fetched_at` itself and writes atomically). **This store has exactly one writer (this step) and one
   reader (the Brief's Phase 1)** — nothing else may read or write it. **Staleness caveat:** the snapshot
   is ~22:00 the night before, so it predates any overnight change — a warm base, not the final word,
   and the Brief runs a light morning **delta** on top (`brief.md` Phase 1), falling back to its own
   live 4-read batch if the store reads stale or absent.
2g. **Rotate the state backups (act-low, local) — FIRST in step 2, before anything reconciles.**
   `python scripts/state_backup.py` (keeps a rolling set of copies of each state file nothing can
   rebuild). **Run it before the reconciles below, not after:** its whole job is to hold the copy that
   predates tonight's rewrites, and a backup taken after `reminders.json` has been reconciled backs up
   the result rather than the input. It stamps `2g` itself, never fails, and always exits 0 — one
   locked file is reported, not fatal. Fold its summary line into tonight's Run Log entry **only if it
   copied nothing or reported an error**. **Do not make these copies by hand** — a hand-made backup runs
   on the nights someone remembers, which is how a destroyed file ends up with only a month-stale copy.
   Which files, which are deliberately NOT covered, and why: `scripts/state_backup.py` +
   `docs/state-durability-spec.md`.
2. **Refresh reminders + prune the local logs.** Reconcile `state/reminders.json`: `python
   scripts/reminders_reconcile.py` (act-low, local) — under the daemon's own queue lock, drops every
   entry whose `due_at` owner-local activity day is before today (an unparseable/absent `due_at` is
   kept, deliberately) and self-stamps `dream_steps.py` step `2`. Left as prose with no owning script,
   the queue grows by hundreds of entries before anyone notices. What's due tomorrow lives in
   `state/reminders.json` itself — nothing to mirror elsewhere. Then, each act-low and local:
   - **Prune stale presence history:** `python scripts/presence_import.py --prune-days 30` (keeps
     `state/presence.db` bounded, the context snapshot untouched).
   - **Sweep the Telegram attachment inbox:** `python scripts/telegram_poll.py --prune-days 30` (no
     network — the inbox is a landing pad, not an archive; `docs/telegram-inbound-spec.md`).
   - **GC finished background jobs:** `python scripts/jobs.py prune --days 14 --mail-days 14` — drops
     terminal jobs whose completion push already landed, with their logs and staged analyses, and sweeps
     `state/session-mail/` on the same retention. A terminal job that never got its ping is deliberately
     kept, so the GC can never be what makes a job go silent — and **mail nobody has read is kept too, at
     any age**, this step being nightly and unattended. A hold is printed (`held N UNREAD past
     retention`) and is worth reading: it means a session that never came back
     (`docs/background-jobs-spec.md`, `docs/job-origin-routing-spec.md`).
   - **Sweep the assertions log:** `python scripts/mouth.py prune --days 30` — what the assistant has
     actually said to the owner (the Mouth; `docs/mouth-spec.md`). A row whose `at` won't parse is kept
     on purpose.
   - **Sweep the merge-ask log:** `python scripts/merge_guard.py prune-asks --days 30` — every approval
     picker that went out. 30 days is far past the approval TTL, so ageing a row can never resurrect a
     question; a row whose `asked_at` won't parse is kept.
   - **Prune the outbox (Notion backend):** `python scripts/outbox.py prune --days 14` — drops `done`
     write-behind entries older than 14 days; dead-letters are never pruned, since only a human
     resolving one should retire it. Filesystem backends have no outbox; skip.
   - **Prune the ad-hoc notes files:** `python scripts/notes.py prune --days 90` — deletes a
     `state/notes/YYYY-MM-DD.md` witness-only note file whose own filename date is more than 90 days old
     (`docs/carry-over-region-spec.md`).
   - **Prune Tomorrow's Lead:** `python scripts/tomorrow_marker.py prune --keep-days 3` — drops
     `done`/`dropped` marks whose `resolved_at` is more than 3 days old (`docs/tomorrow-marker-spec.md`);
     an `open`/`rolled` item is never touched, by construction. **Fail-open:** a missing or unreadable
     `state/tomorrow.json` degrades to an empty store, so this call can never fail the Dream run.
   - **Reclaim the worktrees a job's teardown could not remove:** `python scripts/worktree_gc.py
     --apply` — `jobs.py --worktree` tears down with `git worktree remove` and **never `--force`**, so a
     removal git refuses (usually a file handle still open at that instant) leaves the directory on
     disk; minutes later the same removal succeeds. This is the coming-back-later, not a way around the
     refusal. **It removes only what it can prove is safe** — an empty husk, or a registered worktree
     that is clean *and* whose every commit is pushed — and **leaves and reports everything else**,
     including a running job's tree, anything under the age floor, and any check that errored.
     `--apply` is required because dry run is the default. **Fold its output into tonight's Run Log
     entry only if it removed or refused something**; a `REFUSED` line is worth reading — it means a
     directory holds something no proof could clear, such as a commit that exists nowhere else. Why each
     refusal: the module's own docstring.
   - **Read the transcript archive's size — the one item here that sweeps NOTHING:** `python
     scripts/transcript_size_watch.py check`. `state/transcripts/` is keep-everything by design; this
     deletes not one byte, it is the sensor that pushes the owner **one** message — in code — if the
     archive crosses its size threshold. Fold `fired`/`breached` into tonight's Run Log entry if either is
     true. Never fails, always exits 0.
   - **Measure the positive-resurfacing rate:** `python scripts/owi_resurface.py log` (report-only).
     Appends ONE row to `state/owi-resurface.jsonl`: the rate of owner's-move open-work items the
     assistant has let go past the re-ask threshold without ever raising them (`loops.py raise`). **This
     surfaces nothing and nudges nobody** — it only extends the measurement series; fold tonight's
     `missed`/`eligible` into the Run Log entry only once the series is old enough to be worth
     summarizing.
2i. **Scan the observation gates (act-low, local — `docs/observation-gate-spec.md`).** `python
   scripts/observation_gate.py scan --apply` — checks every register row in `status: "observation"` and
   flips a fully-met gate to `observation-complete` (stamping `gate.met_at`, setting `whose_move`, and
   calling `loops.py raise` so it resurfaces the normal way). **Report every `flipped` id and every
   `stale` row in tonight's Run Log entry** — a flip is exactly the thing this step exists to stop going
   unnoticed (a gate met weeks before anyone saw it), and a `stale` row means the data being waited on
   may no longer be relevant. Never touches a row that isn't `observation`, never closes an item,
   idempotent — a night with nothing to flip is a night with nothing to report.
2b. **Refresh the semantic index (act-low, local — Retrieval advisor phase B).** Incrementally update the
   local RAG index so tomorrow's retrieval has today's history. Fetch journal/notes entries
   new-or-changed since the last index **from the store** (`store-query`/`store-search` the journal &
   notes domains), write them to a JSONL (`{"source","ref","text"}` per line), then run `python
   scripts/rag_index.py --local --chat --ingest <jsonl>`. **`--chat` indexes `state/turns.jsonl`** — one
   document per chat turn, both speakers, so *"you told me last month"* becomes answerable instead of
   *"then it's gone"*; a `!private` turn is skipped entirely, in the indexer as well as the writer
   (incremental — unchanged docs skip; local-only, no outbound). If Ollama/`nomic-embed-text` is
   unavailable, skip the ingest — the index is a regenerable cache and Retrieval falls back to
   `store-search` — **but RECORD the skip, never silently:** `python scripts/dream_steps.py record 2b
   --skip --reason "<why>"`. `rag_index.py` exits 3 without stamping, so a silent skip is
   indistinguishable from a night nobody reached the step. **Probe the port the scripts actually use**
   (`scripts/rag.env`, read from a `__file__`-anchored env file) — a reason citing Ollama's default port
   when the install configures another is a hand-probe of the wrong port, not a down embedder.
   **And "unavailable" has to mean unavailable.** A transport bug can wear that sentence as a costume:
   exit 3 reads identically to a down embedder while the index silently freezes. So the tell is on disk
   — **`dream_steps.py status` shows 2b's real age**, and an exit 3 against a stale 2b row is a bug, not
   a quiet night (`scripts/RAG_SETUP.md`).
   **Refresh the project-state corpus in the same pass:** `python scripts/rag_projects.py --ingest`
   (act-low, local + a read-only `gh repo list`) — rescans the owner's project roots
   (`state/project-roots.json`), re-embeds only projects whose state changed, reconciles vanished ones
   out of the `project` source, and rewrites `state/projects-map.md`. Same skip rule if the embedder is
   down (the JSONL + map still refresh).
   **Compact the mini-dream log in the same pass (the LSM shape).** Include today's new
   `state/session-distillations.jsonl` records in that ingest JSONL (`{"source": "session-distillation",
   "ref": <record id>, "text": <title + distillate>, "provenance": <the record's `provenance` field,
   COPIED VERBATIM>}` — dedupe by `ref`; the index skips unchanged docs), then prune the fast log:
   `python scripts/mini_dream.py --prune-days 30`.
   **COPY `provenance` THROUGH VERBATIM — NEVER INVENT IT, NEVER OMIT IT, NEVER GUESS ONE FOR AN OLDER
   RECORD THAT LACKS IT.** `scripts/provenance_guard.py` refuses any session-distillation record whose
   stamp it does not recognise, so a dropped stamp costs that record's ARCHIVAL and never its safety.
   `llm-excerpt` is refused **by design**, counted in the index's `provenance_refusals` ledger and
   printed by `--stats` — a visible fact, not a missing row (`references/comms-mapping.md`). The
   distillations file is the fast append path (every ended session writes one, via the
   `session_stamp.py` hook); Dream is the slow path that promotes them into semantic recall and GCs the
   tail. Mechanics: `scripts/mini_dream.py`; protocol: `references/memory.md`.
   **Tag each JSONL record for salience while writing it (observe-only).** Add `salience_cat` (the
   closed taxonomy) and, only on eligible categories Dream genuinely predicts it won't need again,
   `disposable: 1` — the rubric is `references/salience.md`. The tag is a *logged prediction being
   scored*, never a hiding: tagged rows stay fully retrievable and `rag_query.py` counts their recalls
   (`scripts/SALIENCE_SETUP.md`). Untagged records are fine; never force one.
2c. **Cross-check new forgetting events (act-low, local — salience Phase 2).** For each
   `state/forgetting-events.jsonl` line appended since the last Dream that has a non-empty
   `reaction_text` and no `classifier_weight` yet, run `python scripts/sentiment.py "<reaction_text>"`
   (local Ollama, abstains gracefully when down) and append the score onto the event line as
   `classifier_weight`/`classifier_confidence` — a second opinion on the recorded `sentiment`, never an
   override. Note any large divergence (|Δ| ≥ 0.4 at confidence ≥ 0.5) in tonight's Run Log entry as a
   data-quality flag. Skip silently if the file is absent or empty; cold start is the normal state.
2d. **Stage meal-plan ideas (act-low, read-only) → `state/meals.json`.** The cockpit's Meals card reads
   a store-staged snapshot of current meal-plan/meal-idea pages — this step is what stages it, nightly.
   `store-search` for pages that look like meal plans/ideas — match on a sensible convention (title or
   tag containing "meal plan"/"meal idea", whatever pages actually exist; **never hardcode a page or
   database id that doesn't exist yet**, same posture as "never load DB schemas at runtime"). For each
   hit, take its title, url, and — only if readily at hand from the search result itself, never a second
   fetch per page — a short one-line summary and/or its tags. **Overwrite** `state/meals.json` with
   `{"staged_at": <ISO local timestamp>, "plans": [{"title", "url", "summary"?, "tags"?}, ...]}` — the
   exact shape `GET /api/meals` reads (`cockpit/server/health.py::read_meals`). Tolerant and fully
   skippable: no matching pages → still write a valid file with `plans: []` and a fresh `staged_at` (an
   honest "nothing staged right now" beats a missing file); the store unreachable/erroring → **skip the
   write entirely** and leave the prior file exactly as it was — its old `staged_at` stands, and the
   cockpit shows that staleness honestly rather than Dream forcing a write it can't back.
2e. **Promote pending archon intel (act-low, local) — only if an archon that keeps an intel overlay is
   deployed** (the shipped example is `../../archons/proteus/`). If
   `../../archons/proteus/state/company-intel-pending.jsonl` has unpromoted entries, run `python
   ../../archons/proteus/tools/promote_intel.py --apply`. It merges them into the archon's local,
   **gitignored** intel file (owner data) — no git, no commit, no PR — and refuses any proposal that
   would weaken an owner entry (it stays queued for the owner). Only an owner who keeps the ledger in
   a **private fork** adds `--via-pr` (it publishes the file to the checkout's remote): that opens an
   **ask-high** PR the merge guard HOLDS for the owner's approval — Dream records it as
   held-for-approval and moves on. Nothing pending → nothing to do, as most nights. No tracked file
   changes under the live checkout. Skip silently if the archon isn't deployed or the queue is gone. **The script
   stamps 2e itself on a successful run**, including a nothing-pending one — so running it *is* what
   clears the ledger row, and a `--dry-run` stamps nothing. The 2e row only ALARMS where the archon is
   deployed (its gitignored `profile.json` exists); elsewhere `status` lists it but never nudges.
3. **Propose learnings (gated).** Spot repeated patterns worth encoding (*"declines every recruiter
   invite," "archives the X newsletter every time"*) and draft each as a **proposal**. **Weekly, also run
   the Observability rollup:** scan a rolling window of `state/metrics.jsonl` (the metrics the
   Observability advisor emits) and, when an ask-high action has been surfaced N times and **repeatedly
   `approved_as_is` with 0 `edited_before_approve`/`rejected`**, draft an ask-high → act-low **graduation
   proposal** — same gated path, evidence cited from the metrics. **Weekly, also run the salience
   rollup:** `python scripts/salience_rollup.py` (report-only; add `--propose` for draft text) — it
   buckets each category off the access counters + forgetting-events and **abstains on insufficient
   data** (the normal state for the first ~month). Fold the report into tonight's Run Log entry; if it
   drafts a prune proposal, carry that text into `references/proposed-learnings.md` via step 5's PR — **a
   prune proposal is act-high like every other**, and its first applied form is the reversible
   `disposable=2` soft mark, never a delete (`references/salience.md`). **Never change behavior
   autonomously** — a proposal becomes policy only on the owner's approval, at which point the assistant
   applies it (edit `autonomy-config.json` / `autonomy-policy.md` / the relevant reference / persona) and
   logs it in the `autonomy-policy.md` graduation log. Surface each new proposal to the owner (Telegram /
   the store / next Brief). Its text lands in `references/proposed-learnings.md` via step 5's PR — never
   in the daemon's live checkout. Format: that file.
4. **Leave a trace.** Write a Run Log entry (mode: Dream) summarizing the consolidation + any proposals
   (the store's Run Log domain + the `state/run-log.md` mirror).
4d. **Reconcile the held queue before re-raising anything from it** (act-low, local, no network): `python
   scripts/learnings.py audit`. It lists open proposals whose `Applies to:` file has changed **since the
   proposal was raised** — a reason to look, never a verdict, so it reports and never edits. Anything it
   flags: check whether the fix actually landed, and if it did, close the row with `python
   scripts/learnings.py close <date> --note "applied in <commit>"` rather than re-proposing it. Right
   after: `python scripts/learnings.py retire --apply` — moves any row that is closed, stamped
   (`close`/`restamp` write the stamp; an older unstamped row is left alone), and at least 30 days old
   into `references/proposed-learnings-archive.md` (created on first use), verbatim, never deleting it.
   Provably safe by construction (only a stamped, aged row is ever touched), so it runs with `--apply`
   here rather than needing a human to confirm the plan first.
   **Why:** closing a row used to be a thing someone had to *remember*, so an already-applied proposal
   got re-raised night after night as if it were open — and closing a row never shrank the file, so it
   only ever grew. The measured case and both halves of the fix: `scripts/learnings.py`'s module
   docstring and `docs/proposed-learnings-lifecycle-spec.md`.
5. **Open a PR only if a *tracked source* file changed — then merge it on green.** The memory Dream
   writes (`state/open-loops.json`, `state/run-log.md`, `state/carry-over.md`,
   `state/standing-safety.json`, `state/brief-prestage.json`) is **gitignored** — nothing to commit for
   it, and **most nights no PR at all**. The only thing worth a PR is a genuine **source** change: a new
   proposal for `references/proposed-learnings.md` (or, on the owner's approval, an applied edit to a
   policy/reference/persona/skill). If this run produced none, **skip this step** — Dream wrote its
   caches + the store and is done. When there IS one, build the commit + PR **without disturbing the live
   daemon** (it runs off the deployment branch — `main` by default; never `git checkout`/switch its
   branch) — use a **transient worktree** (every git command with `-c core.fsmonitor=false`):
   1. `git -c core.fsmonitor=false worktree add -b seneschal/dream-<YYYY-MM-DD> <tmp-dir> origin/main`
   2. In that worktree, **write the change** (e.g. append the new proposal to
      `references/proposed-learnings.md`), stage **only** that file (never `git add -A`), and commit —
      Conventional-commit message (scope `feat(learnings):` / `chore(...)`) ending with the
      `Co-Authored-By: Claude <noreply@anthropic.com>` trailer.
   3. `git -c core.fsmonitor=false push -u origin seneschal/dream-<date>`, then ensure a PR against the
      **deployment branch** (the branch the daemon runs from — `main` by default; `seneschald-update`
      pulls it after merge): `gh pr list --head seneschal/dream-<date> --state open` → if none, `gh pr
      create --base main` (body ends with the `🤖 Generated with [Claude Code]` line); if one exists, the
      push updated it — reuse it.
   4. **Merge on green.** Watch CI on the PR's head commit (`gh pr checks <n> --watch`, or poll `gh pr
      view <n> --json statusCheckRollup`). **Every check green → `gh pr merge <n> --merge`** (a merge
      commit — never squash, never rebase). **Any check red or still pending → do not merge, no
      exceptions**: pending is *unknown*, and unknown fails closed; red is never waived, even a
      seemingly unrelated flake. If the base branch has since been fixed, rebase onto it, force-push the
      Dream branch and let CI re-run — a genuinely green merge is the only kind. Otherwise leave the PR
      open, record the failure in the Run Log, and surface it to the owner (Telegram / next Brief). If a
      prior night's Dream PR is still open (held on red), fold its still-relevant content into tonight's
      branch and close the stale one as superseded.
   5. `git -c core.fsmonitor=false worktree remove <tmp-dir>`, then record the PR URL + outcome (merged
      / held-on-red / held-pending) in the Run Log entry.
   The merge is the assistant's to make **only when CI is green**, and it only *records* the proposal —
   it does **not** apply it. After the merge, `seneschald-update` pulls and gracefully reloads the daemon
   (`references/memory.md` / `scripts/seneschald-control.ps1`).

Dream is **summarize-and-propose only** (step 5 for the PR path). Merging *records* a proposal;
**applying** one stays gated on the owner's explicit approval. Dream never sends outbound to third
parties and never edits the owner's data (tasks, calendar, email). The live checkout stays on the
deployment branch and clean; `seneschald-update` picks the merged change up automatically.
