# Jobs panel depth — how much of Claude Code's "Background tasks" belongs in the cockpit

**Status:** `SPEC-ONLY(§7 superseded)` — nothing built; §7 is superseded by `job-fanout-spec.md`. ·
**Scope:** `cockpit/server/jobs.py` (the tolerant jobs reader + `GET /api/jobs` /
`GET /api/jobs/{job_id}`, which ship), the cockpit's Jobs **web** panel and its `Job` type (a later
component — the panel-side rows below describe its design), and — for one phase only, and only if it is
taken — two lines of `seneschal/scripts/jobs.py`.

**The ask.** The owner shared a screenshot of Claude Code's *Background tasks* panel and asked how much
of it the cockpit's Jobs panel could implement — then asked for it to be specced.

**What this document is for.** Not "copy the panel". It answers, per element, *which bucket does this
fall in for a job model where a job is one detached OS subprocess* — already built, one field away,
real work, or a new capability that changes what a job **is**. Three of the four buckets are cheap.
The fourth is the phases/agents tree, and **§7 recommends against building it**, with the measurement
that makes that the honest answer rather than the lazy one.

Design context, not restated here: the jobs mechanism is `background-jobs-spec.md`; the cockpit's
conventions are `cockpit-spec.md` and `cockpit/CLAUDE.md`.

> **AMENDED. §7's recommendation is superseded; read `job-fanout-spec.md` before acting on it.** The
> owner pushed back — they want the agent breakdown for build jobs, and assumed something in the
> pipeline can already run several agents at once. **They are right, and §7.4's reasoning was
> circular.** It measured a population in which nothing fans out and concluded nothing would ever be
> there to show; but the reason nothing fans out is that the briefs never ask, not that the capability
> is missing. **The `Agent` tool is present and unrestricted in a delegated `claude -p` run and had been
> used in exactly one job worktree — that job spawned 9 concurrent agents and produced a wide PR that
> merged.** Two specific corrections, both measured in `job-fanout-spec.md` §1:
>
> - **§7.3's emit protocol is not needed at all.** The CLI already writes
>   `<projects>/<encoded-cwd>/<session>/subagents/agent-*.{meta.json,jsonl}`, from which agent, kind,
>   model, elapsed, tokens and tool counts are all derivable with **no cooperation from the job**. Its
>   two `child_env()` lines are not the join either — `CLAUDE_CODE_SESSION_ID` is *set* by the CLI, not
>   read from it. The join is the `--worktree` path, which already spells the job id: **every worktree
>   job that ran more than a few seconds resolves to its transcript directory.**
> - **§6's "not sourceable" over-reaches.** The `metrics.jsonl` finding is exactly right and stands.
>   But a delegated `claude` job's own transcript carries `usage` on every assistant message, per
>   agent — so tokens *are* sourceable for the population the tree is for. §6's recommendation against
>   a token *column* stands on its other reasoning; its blanket claim does not.
>
> §7.3's **reader** rules and §7.4's absence invariant survive unchanged and are reused verbatim.
> §8.1 (Clear, rejected) and §8.2 are untouched.

---

## 1. The measurements this rests on

Read-only scans of a live daemon's `state/` directory. The counts were taken on one install and are not
reproduced here; each row records the **structural finding** it established.

| # | Measurement | Finding |
|---|---|---|
| M1 | Files in `state/jobs/*.json` | job records, plus a few non-job files other tools write into the same directory |
| M2 | …of which are **job records** (have a string `id`) | the rest (e.g. archon lead files keyed `job_id`) are dropped by `cockpit/server/jobs.py::_normalize`, correctly, for having no `id` |
| M3 | Records with a non-empty `origin` | the large majority, since origin is auto-stamped |
| M4 | Records with `origin.goal` | **every** record that has an origin at all has a goal, a session id **and** `stamped_by: env` |
| M5 | Recent records with `origin.goal` | nearly all |
| M6 | `origin.goal` length | the maximum observed **is** the cap |
| M7 | Goals sitting **exactly at the 200-char cap** (`jobs.ORIGIN_GOAL_MAX`) — i.e. truncated | **about a third** |
| M8 | `argv[0]` across records | a shell wrapper (`pwsh`/`bash`) most often, then `python`, then `claude`, then `wsl`/`uv` |
| M9 | Records whose `argv` mentions `claude` **anywhere** | a minority |
| M10 | Recent records whose `argv[0]` is `claude` | **none** |
| M11 | Records passing `--output-format` | **none** |
| M12 | Records passing `--model` | **none** |
| M13 | Scratch launcher scripts that invoke `claude` | most of them; the launch line is `& claude --permission-mode bypassPermissions -p $brief` |
| M14 | Rows in `state/metrics.jsonl` | one per warm-session chat turn, none unparseable |
| M15 | Distinct `writer` values in `metrics.jsonl` | **1** — `daemon`. `mode` is `Chat` on every row; `source` is `telegram` or `cockpit` |
| M16 | Rows in `metrics.jsonl` carrying `tokens` / `usage` | nearly all |
| M17 | Keys in `metrics.jsonl` naming a job | **none** |
| M18 | Records that ran in a private worktree | a large minority |
| M19 | Job duration | median minutes, p90 under an hour, a tail running many hours |
| M20 | Peak **concurrent** jobs, all-time | single digits |
| M21 | Job logs on disk | median a few KB, max a few hundred KB |

M8/M10/M13 together are the finding that governs half this document, so it is worth stating in one
line: **the dominant delegated-job shape is not `claude` — it is a shell wrapper
(`pwsh -NoProfile -File <scratch>.ps1`) whose script calls `claude` inside.** From `jobs.py`'s point of
view that job is a shell script. None of the recent jobs are `claude`-first.

## 2. The bucket table — every element of the screenshot

The screenshot, read element by element. Bucket key: **A** = already built · **B** = one field away ·
**C** = real work, tractable · **D** = a new capability, not a display change. For a panel-rendering
element, **A** means the server field ships in `cockpit/server/jobs.py` and the web panel's design
renders it as described.

| Screenshot element | Bucket | Why, grounded in the code |
|---|---|---|
| Header "Background tasks" + a *Running* section | **A** | `read_jobs` returns `active` (running, oldest first) and `recent` (finished, most-recently-ended first); the panel renders them as two groups. |
| Task name | **A** | `title`, falling back to the job id (`_normalize`). |
| Elapsed time on a running task | **A** | `_duration_sec` computes elapsed for a running job and total for a finished one; the panel renders them differently on purpose — *"running 30m"* vs *"took 30m"*, so a finished job never reads as "30m ago". Whether Claude Code's finished view draws the same distinction is not visible and is not claimed here (§9). |
| Per-task status | **A** | `status` + a status glyph, with an unrecognized status passed straight through rather than coerced — the backend must survive a daemon on a different commit. |
| Exit code | **A** | Rendered, and deliberately **only when non-zero** — a `· exit 0` on every finished row is noise. |
| `lease` / `wake` flags | **A** | Rendered as ` · lease` / ` · wake`. No counterpart in the screenshot. |
| Expandable detail | **A** | The row's open state; shows `command`, `error`, started/ended/notified timestamps. |
| **A log tail** | **A**, *and better than the screenshot* | `log_tail()` — last N non-empty lines, last 64 KB only, unreadable/binary → `[]`. Claude Code's panel offers no output at all. This is the single largest capability the cockpit has that it doesn't. |
| Recently-finished collapse | **A** | A `recently finished` group with a `show N older` toggle, persisted per viewer. The screenshot's `Finished N >` is a drill-in; this is an in-place expand. Same job. |
| **`awaiting_push` alarm** | **A**, *and it has no counterpart at all* | A terminal job whose completion push never landed. `jobs.py` stamps `notified_at` only on a send that actually succeeded, so a non-zero count means the daemon is trying to reach the owner and **failing** — the exact silence the whole jobs feature exists to end. It is the panel's reason to exist and the only thing styled as an alarm. |
| **The grey intent paragraph** | **B** | `origin.goal` is on the large majority of records (M3/M4) and `_normalize` does not emit it. One field on the wire. **§3.** But see M7: about a third of goals are cut at 200 chars. |
| Kind label ("Workflow") | **B-ish, and the honest answer is "we don't have this"** | Nothing in the record says what kind of thing a job is. `origin.source` (daemon / desktop / scheduled) says who **asked**, which is a different axis, and deriving kind from `argv[0]` would label the dominant delegated job (M13) a shell script. **§5 — ride along with phase 1 as `origin.source`, and do not invent a "Workflow" kind.** |
| Per-agent **Time** | **A / D**, depending on the row | Whole-job time: already built. Time *per agent row*: only exists if the agent rows exist (§7). |
| Per-agent **Model** | **C→D** | Nothing records a model. `--model` is passed by no job (M12), and the model an in-script `claude` picks is invisible to `jobs.py`. **§6.** |
| Per-agent / aggregate **Tokens** | **D, and NOT via `metrics.jsonl`** | **§6** — the claim the first answer got wrong. |
| Aggregate meter "N agents · M tokens" | **D** | Derived from the agent rows; no agent rows, no meter. |
| **The Phases tree + agent tables** | **D** | A job is one opaque OS subprocess with no phase concept, no sub-agent registry and no structured progress channel. **§7 designs the emit protocol and then recommends against building it.** |
| "Clear" | **REJECTED** | **§8.1.** |

## 3. Phase 1 — the goal passthrough (recommended; do this first)

**What it buys.** The panel currently shows *what ran* (`pwsh -NoProfile -File …\<scratch>.ps1`) and
never *what it was for*. That is the difference between a row you can act on and a row you have to go
read a scratchpad script to understand. The data is already on disk and stamped automatically — nearly
every record carries it, with no caller having to remember anything (`jobs.build_origin`,
`stamped_by: env`).

**Real example.** The job that produced this document renders as its title plus a `pwsh` command line.
Its `origin.goal` reads:

> *Spec how much of Claude Code's Background-tasks panel belongs in the cockpit Jobs panel - phased,
> cheapest first, and honest about what our single-command job model cannot support*

**Fields.** `goal` plus a normalized `origin` block. The goal is the grey paragraph; the rest lives in
the expanded detail, because "which session started this, on which branch" is the question you ask
*after* something looks wrong, not at a glance.

| Wire field | Source | Rendered where |
|---|---|---|
| `goal` | `origin.goal` | Under the title, grey, always visible on the row |
| `origin_session_id` | `origin.session_id` | Expanded detail |
| `origin_source` | `origin.source` | Row sub-line — see §5 |
| `origin_branch`, `origin_cwd` | `origin.branch` / `origin.cwd` | Expanded detail |
| `origin_stamped_by` | `origin.stamped_by` | Expanded detail. Cheap provenance: it is how you find out whether the auto-stamp is still firing before something depends on the answer |

**Files, and roughly what changes in each.** This is an honest sizing, not a slogan.

| File | Change |
|---|---|
| `cockpit/server/jobs.py` | One tolerant `_origin(rec)` helper (~10 lines) + six keys in `_normalize`'s returned dict. `origin` absent, `{}`, or not-a-dict → all six null; a non-string value → null, never coerced. **The reader re-truncates `goal` itself** rather than trusting the writer's cap — the precedent is `readers.py::read_router_stats`, which re-truncates `text_preview` defensively rather than trusting that boundary to hold forever. M7 is why that instinct is right: writer caps bind, and a different `jobs.py` has a different one. |
| `cockpit/server/test_jobs.py` | ~5 cases: goal present; `origin: {}` → nulls; `origin` not a dict → nulls; a goal longer than the reader's cap is truncated **by the reader**; a record with no `origin` key at all reads identically to `{}` (that equivalence is `jobs.normalize_origin`'s stated rule and every reader must honour it). |
| the web panel's `Job` type | Six optional fields. **Optional, not required** — an older daemon's backend omits them and the panel must render without. |
| the web panel's row | One goal paragraph under the title; four lines in the detail block. **The row's "has detail" test gains the origin session id** so a job with an origin but no log is still expandable. |
| the web panel's styles | One `.job-goal` rule beside the existing job title/command rules. |
| `seneschal/docs/jobs-panel-depth-spec.md` + `seneschal/docs/CLAUDE.md` | Status flips to phase 1 built; router row updated. |

**Estimated size: ~80 lines of implementation and ~60 of test.** That is an estimate; it rests on the
field count above and on `_normalize` already being the single place every job field is shaped. Nothing
about it is speculative — the data exists, the reader exists, the row design exists.

**What phase 1 does NOT fix.** About a third of goals are cut at exactly 200 characters (M7) and end
mid-clause, so the grey paragraph will read as a fragment about a third of the time. That is a
**writer**-side cap (`jobs.ORIGIN_GOAL_MAX = 200`) and raising it:

- is a one-constant change in `seneschal/scripts/jobs.py`, and
- **cannot retro-fill.** `normalize_origin` applies the cap at the single write point, so goals already
  truncated on disk are truncated forever.

**Recommendation, the owner's to decide:** raise `ORIGIN_GOAL_MAX` to **400** in the same PR as phase 1.
The cap exists to stop a caller pasting four paragraphs into a ledger field, and 400 still does that
while clearing the observed maximum with room. It is not free — the goal also rides the completion push
and the `jobs.py status` output — but 400 characters is roughly three lines of Telegram. **What a
400-char goal does to the push's shape is unmeasured, and this document does not claim otherwise.**

## 4. Phase 2 — the expanded row reads the detail endpoint (recommended; ~20 lines)

**A verified oversight, not a new feature.** `cockpit/server/jobs.py::read_job` and
`GET /api/jobs/{job_id}` exist, are tested, validate the id against `_SAFE_ID` before touching the
filesystem, and serve a **60-line** tail (`DEFAULT_DETAIL_LINES`). In the panel's reference
implementation the web client exported a `getJob` wrapper and **nothing called it**: the panel polled
the list only, so expanding a row showed the **3-line** list tail (`DEFAULT_TAIL_LINES`) — the same
three lines that were already on screen. When the web panel lands here, it should not repeat that.

So the panel's answer to *"it failed, why?"* would be three lines, while a twenty-fold better answer is
built, tested, routed and reachable.

**Change:** on expand, fetch `GET /api/jobs/{job_id}` once and render its tail in place of the list
tail; fall back to the list tail if the fetch fails or returns `available: false`. One piece of row
state + one effect. **Do not poll it** — expanding is a deliberate act and the list already refreshes
every few seconds; a second polling channel per open row is how a read-only dashboard becomes a load
source. Logs are a few KB at the median and a few hundred KB at the max (M21), so a one-shot fetch is
cheap; the reader caps its own read at 64 KB regardless.

This phase is independent of phase 1 and could ship first. It is listed second only because phase 1
answers a question the panel is asked more often.

## 5. The kind label — ride along with phase 1, and do not invent it

The screenshot's `Workflow` is a **true** statement about a Claude Code task, because Claude Code's
orchestrator minted the workflow and knows it did. We have no equivalent fact.

Three candidate sources, and why two of them are wrong:

1. **Derive from `argv[0]`.** Refused. It is *available* (M8) and it is *misleading*: most scratch
   launcher scripts invoke `claude` inside (M13), so the delegated agent job that dominates recent
   traffic would be labelled "script", and **no recent job would be labelled "claude" at all** (M10). A
   label that is wrong on the majority case is worse than no label, because it is *believed*.
2. **Add a `--kind` flag to `jobs.py start`.** Refused, on this repo's most expensive recurring lesson:
   a field a caller must *remember* to populate is not a mechanism. `origin` stayed empty on every
   record until it was auto-stamped; `metrics.jsonl`'s job-side rows never appeared. `build_origin`'s
   own docstring says it: *"the model that forgets `--wake` will forget `--origin-session`."*
3. **Ship `origin.source` and call it what it is.** Recommended. It is already stamped automatically
   from the session registry, and it answers a real question — *daemon* / *desktop* / *scheduled*, i.e.
   did the assistant start this itself, did one of the owner's sessions start it, or did a scheduled
   task. It is **not** the screenshot's "kind" and the UI must not present it as one; it goes in the row
   sub-line as a plain word, next to `lease` / `wake`.

**This rides along with phase 1** — it is one more field out of the same `origin` block, and giving it
its own phase would mean touching the same files twice.

## 6. Model and tokens — what is actually possible, measured

**This is where the first quick four-bucket answer was wrong, and the correction matters more than the
original claim.**

**What was said:** *"`metrics.jsonl` carries tokens workspace-wide — the question is whether per-job
attribution is possible from it."*

**What is true.** `state/metrics.jsonl` has **exactly one writer** — the daemon's per-turn metrics
append — and it writes **one row per completed warm-session chat turn**. Every row carries
`writer: daemon` and `mode: Chat`; `source` is `telegram` or `cockpit`. There is no job-shaped row in
the file and no key that could name a job (M14–M17). So:

> **Per-job token attribution from `metrics.jsonl` is not possible today, and it is not one
> correlating id away either. A correlating id joins rows that exist. These rows do not exist.**

Two secondary findings from the same read, both worth recording:

- **`cockpit/server/readers.py::_extract_tokens`'s docstring is stale** once the daemon writes token
  fields. It says the current schema has no token field at all and that this is forward-compatible
  plumbing. Where live rows carry `tokens` / `usage` (M16), `read_usage`'s `tokens_available` is
  `true` and the comment should say so. Docs-only; correct it in whatever PR next touches that module.
- The usage aggregator counts *every* row in `metrics.jsonl` as plan usage — a limitation the daemon
  already names against itself. Nothing in this spec changes it.

**So which job kinds could ever populate model/tokens?** Only a job that *is* a `claude` CLI run, and
only if launched to report machine-readable usage:

| Shape | Share | Can it yield tokens? |
|---|---|---|
| `claude -p …` directly as `argv` | a minority historically, **none recently** (M8, M10) | **Yes, with `--output-format json`** — the CLI then emits a result object carrying usage. **No job passes it today** (M11). |
| a shell wrapper whose script calls `claude` | the dominant recent shape (M13) | **Only if the wrapper script cooperates** — it would have to run `claude` with a machine-readable output format and write the usage somewhere `jobs.py` can find. That is a per-script contract, i.e. exactly the prompt-side contract §5(2) refuses. |
| `wsl bash …`, `python <watcher>.py`, `uv …` | most of the rest | **No, and never.** There is no model and no token count because there is no model call. |

**The cost of launching differently, stated plainly.** Adding `--output-format json` to the direct
`claude -p` shape is not free and is **untested**:

1. It changes what lands in the job log from human-readable prose to a JSON blob. That log is rendered
   in the panel, quoted in the completion push's *"Last line:"*, and fed to `--analyze`.
2. It is read at column 0 by two existing classifiers — `FAILURE_ASSERTIONS` (the
   `background-jobs-spec.md` §3.11 exit-0-vs-the-log guard, whose window is *this attempt's last few
   lines, column 0*) and `TRANSIENT_SIGNATURES` (the retry classifier). Changing the log's shape
   changes their input.

**Recommendation: do not do this.** The reachable population is small and historical; the blast radius
is the mechanisms above; and the thing it buys — a token count on a minority of rows — is a number, not
an answer. If the owner wants per-job spend, the cheaper and more honest route is a **separate**
measurement question (what did delegated work cost this week?), not a column in this panel. That is out
of scope here and this spec does not design it.

## 7. The phases / agents tree — designed, then recommended against

### 7.1 What it would take, structurally

A job is `subprocess.Popen(rec["argv"], …, stdout=<log fd>, stderr=subprocess.STDOUT)` inside
`jobs._run_shim`. Two consequences decide the whole design:

- **There is exactly one output stream.** stderr is merged into stdout into one file. There is no
  spare fd and no side channel.
- **The child did not know its own job id.** `jobs.child_env()` was `os.environ.copy()`, minus
  `ANTHROPIC_API_KEY`, plus `PYTHONUNBUFFERED=1`. (It has since gained `SENESCHAL_JOB_ID`, for the
  background guard — `background-jobs-spec.md` §3.14 — which is half of §7.3's writer side, landed for
  an unrelated reason.)

So a job cannot describe itself even if it wanted to. Making the tree real means jobs **emit** progress
and the panel parses it.

### 7.2 Channel (a) — sentinel lines in the existing log. REFUSED.

A line like `@@seneschal:progress {"phase":"Verify",…}` at column 0. Attractive: zero plumbing, works
from `echo`, `Write-Output` and `print()` alike, needs no change to the shim at all.

Refused for three reasons, all of them about that log already having jobs:

1. **The log is untrusted text that we display.** A job that `cat`s a file containing the sentinel
   forges a phase. Not hypothetical — jobs crawl third-party text and read PR diffs.
2. **Two classifiers already read that stream at column 0.** `FAILURE_ASSERTIONS`
   (`background-jobs-spec.md` §3.11) and `TRANSIENT_SIGNATURES` (§7.2 of the same spec). Adding a
   third column-0 semantic makes progress output an input to the exit-code and retry decisions. Those
   two guards were each bought by a real incident; feeding them new noise to save a file handle is a
   bad trade.
3. **The tail is quoted to the owner.** The completion push quotes the log's last line. A job that
   happens to end on a progress line pushes machine noise to Telegram.

### 7.3 Channel (b) — a sidecar file. The design, if it is ever wanted.

`state/jobs/<id>.progress.jsonl`, written by the child, read by `cockpit/server/jobs.py`.

**Writer side — the entire change to `seneschal/scripts/jobs.py`:** `child_env()` exports two variables.

```
SENESCHAL_JOB_ID        = <job id>          (already exported — see §7.1)
SENESCHAL_JOB_PROGRESS  = <state_dir>/jobs/<id>.progress.jsonl
```

Two lines, in the one function that already builds the child's environment, alongside
`PYTHONUNBUFFERED`. A job that ignores both is unaffected in every observable way.

**Event schema** — one JSON object per line, append-only:

```
{"at": "<iso8601>", "phase": "<str>", "agent": "<str>", "status": "start"|"ok"|"fail",
 "model": "<str, optional>", "tokens": <int, optional>}
```

**Reader rules** (all of them existing precedent in this codebase, not new invention):

- Read at most the last 64 KB, like `log_tail`'s `TAIL_READ_BYTES`; keep at most the last 200 events.
- Parse line by line; **an unparseable line is skipped and does not count against the limit**, exactly
  as `readers.py::read_oneiroi` does. A job killed mid-write leaves a partial final line and that must
  cost nothing.
- Type-check every field; a wrong type drops to `null` rather than passing through — the tolerant-reader
  posture of `cockpit/CLAUDE.md`.
- **Truncate every string in the reader.** Never trust the writer's cap (§3, and M7 is the evidence
  that caps bind and differ).
- **Derive the completion fraction from the events**, never from a `total` the job asserts. A job that
  claims `3/1` must be impossible to render, not merely unlikely.
- Render as text. React escapes by default; the rule exists so nobody later "improves" it with
  `dangerouslySetInnerHTML`.

**The invariant that makes this safe to ship at all:**

> **A job that emits nothing renders exactly as it does today.** No `Phases` header, no empty tree, no
> `0 phases` chip, no placeholder. The absence of a sidecar file is not a state worth drawing.

That is not a nicety. On day one it would be **at least four fifths of jobs** — every shell, `python`,
`wsl bash` and `uv` job in the store — and **every recent one**. A panel that grows a broken-looking
affordance for four fifths of its rows is a regression, whatever it adds for the fifth.

### 7.4 Why this recommended NOT building it

> **SUPERSEDED — `job-fanout-spec.md`.** Every finding below is correct and the conclusion does not
> follow from them. Read it as the argument it is: *a tree of a job that did not fan out shows
> nothing*, which is true, and which says nothing about whether jobs should fan out. The three
> paragraphs that follow are kept rather than rewritten, because the second one — *"populating it
> would be a prompt-side contract"* — is still right and is why the replacement design **reads** the
> breakdown out of the CLI's own transcripts instead of asking a job to emit it.

**The tree describes a job model we do not have.** Claude Code's card shows agents under phases because
its orchestrator *minted* those agents. A job here is one command. The honest rendering of today's jobs
is a tree with one phase containing one agent — which is strictly *less* information than the log tail
already on screen, since the tail is real output rather than a self-report.

**Populating it would be a prompt-side contract, and this repo has paid for that repeatedly.** Nothing
in `jobs.py` can know a job's phases; only the brief can. So the tree gets filled by writing *"emit a
progress line at each phase"* into every delegated brief — the identical shape as `metrics.jsonl`'s
job rows (never written), `origin` (empty until auto-stamped), and a Dream step prompt that silently
skipped sub-steps. Each of those has a spec explaining why. Another would be a choice, not an accident.

**The value is already delivered by cheaper things.** What the tree is *for* is "where has this
five-hour job got to". That is answered by the live log tail (`background-jobs-spec.md` §3.8 made the
log live precisely so a running job could be watched), and phase 2 makes the expanded answer 20× longer
for ~20 lines of code. The tree adds structure to information we already show.

**The recommendation, plainly: build phases 1 and 2. Do not build the tree.** If a long job ever
genuinely needs a stable progress line that does not scroll away, the answer is **one line, not a
tree**: the §7.3 sidecar with a single `{"at","note"}` event rendered as a `now: …` sub-line. That is a
fraction of the work and it fits a single-command job model honestly. It should be built **when a job
asks for it**, not before — and the trigger is a real job, not a plan.

## 8. Non-goals

### 8.1 "Clear" — rejected

The screenshot's footer carries a **Clear** button that discards finished-task history. It will not be
built, and this section exists so a future session does not helpfully add it back.

**The reason is not aesthetic.** The Jobs panel is read-only by decision — no start, no cancel —
because a dashboard button that kills or discards work is a one-click accident. **A history-discarding
button is a policy change, not a UI one.** The jobs ledger is the durable record behind *"I'll tell you
when it's done"*; `awaiting_push` is computed from terminal records that are still sitting there, and
retention is `background-jobs-spec.md` §3.7's decision, not a viewer's. A Clear button would let a
misclick delete the evidence that the daemon failed to reach the owner — which is the one thing this
panel exists to show.

**And nothing wants it.** The equivalent of "dozens of finished tasks are cluttering the view" is the
recently-finished collapse, which by design can never hide a running job or an undelivered push (the
panel's collapse rule, pinned by its own test).

### 8.2 The rest

- **Any write affordance, in any phase.** No start, no cancel, no retry, no clear, no edit. Cancelling
  is `jobs.py cancel`, a decision made with a reason attached (`cancel-attribution-spec.md`).
- **Anything that changes what a job IS.** No sub-job registry, no orchestrator, no parent/child job
  graph, no phase state machine in the record. The one writer-side change any phase here may make is
  §7.3's environment variables, and that only if the tree is ever taken.
- **`awaiting_push` may never become collapsible, de-emphasised, nested inside a new tree, or pushed
  below the fold by any layout in this document.** It is pinned by the panel's collapse rule and its
  test; a new layout must not route around the pin.
- **Heuristically inferring phases by parsing arbitrary job output.** A regex over untrusted text that
  guesses at structure would be confidently wrong on jobs that merely print section headers, and it
  would be wrong *decoratively*, which is the worst kind.
- **A per-job cost or token column sourced from anything other than a real per-job measurement.**
  §6. An estimate rendered in a column reads as a measurement.

## 9. Where this document is uncertain

Stated rather than smoothed over:

- **`--output-format json`'s effect on the log, the push and the two classifiers is UNTESTED.** §6
  reasons about it from reading `_run_shim`, `FAILURE_ASSERTIONS` and `notify_text`; no job was run
  with the flag. The recommendation to skip it does not depend on the answer, but the described cost
  does.
- **The `ORIGIN_GOAL_MAX` → 400 recommendation is a judgement, not a measurement.** It clears the
  observed maximum with room, but the observed maximum *is* the cap (M7), so the true distribution of
  what callers would have written is unknown — and unknowable from records the cap already truncated.
  A raise would have to be re-measured a fortnight later to learn the real shape.
- **Phase 1's ~80/60-line sizing is an estimate.** It rests on the field count in §3 and on
  `_normalize` being the single shaping point. It has not been implemented.
- **The four-fifths "would emit nothing" figure is a floor, not a point estimate.** It counts every job
  whose `argv[0]` is not `claude`. Some `claude`-first jobs would also emit nothing, so the true
  fraction is higher; a floor is enough to make §7.3's invariant non-negotiable.
- **`read_jobs` cost at fleet scale is untested.** Phase 1 adds no disk read. §7.3's sidecar would add
  one `stat` + bounded read per job per poll. Peak concurrency is single digits (M20) and the reader
  already does one log read per job, so this is very likely a non-issue — but it is reasoning, not a
  benchmark.
- **This spec does not know what Claude Code's panel does on failure**, because the screenshot shows
  one running task. Whether its finished/failed presentation has anything we lack is unexamined.

## 10. Summary

| Phase | What | Bucket | Recommendation |
|---|---|---|---|
| 1 | `goal` + the `origin` block on the wire and on the row; `origin.source` rides along as the honest not-quite-"kind" | B | **Build.** Biggest gain per line in the document; the data is already on disk. |
| 1b | Raise `ORIGIN_GOAL_MAX` 200 → 400 in the same PR | — | **Recommended, the owner's to decide.** Cannot retro-fill already-truncated goals. |
| 2 | The expanded row calls the detail endpoint that already exists | A (unwired) | **Build.** ~20 lines; turns a 3-line answer into a 60-line one. |
| — | Model / tokens columns | D | **Do not build.** Not sourceable for at least four fifths of jobs, and `metrics.jsonl` cannot supply it for any of them. |
| — | Phases / agents tree | D | ~~Do not build.~~ **SUPERSEDED — `job-fanout-spec.md`.** Not a tree and not an emit protocol: an **agent list read out of the CLI's own `subagents/` transcripts**, joined to the job by its `--worktree` path. Gated behind that spec's phase 1 (two sentences next to `jobs.py`'s caller-facing rules), which is the part that changes what happens. |
| — | Clear | — | **Rejected**, §8.1. |

## Router entry

**Status:** SPEC ONLY — nothing built; §7 SUPERSEDED by `job-fanout-spec.md` (its §6 "not sourceable"
over-reaches too — the metrics.jsonl half stands, the blanket claim doesn't).

**What it decides:** How much of Claude Code's *Background tasks* panel belongs in the cockpit, per
element — **and it talks two of the four buckets down.** The grey intent paragraph is one field
(`origin.goal`, auto-stamped, on nearly every record), but **about a third sit exactly at the 200-char
cap**, so it lands truncated a third of the time and no raise retro-fills. **`metrics.jsonl` cannot
supply per-job tokens and is NOT one correlating id away** — one writer, one row per warm CHAT turn,
zero job rows: an id joins rows that exist. **Kind is underivable**: `argv[0]` reads as a shell for the
shape that dominates (scratch launchers call `claude` inside; **no recent job is `claude`-first**), so
the honest field is `origin.source` — who ASKED, not what it IS. §7 designs the phases/agents emit
protocol (a sidecar file, **never** log sentinels — that stream is untrusted, quoted to the owner, and
already read at column 0 by two classifiers) then **recommends against building it**: a one-command job
is one phase of one agent, less than the log tail already shows, and filling it is a prompt-side
contract. Free finding: **the detail endpoint had no caller**, so an expanded row showed 3 lines where
60 are built and routed. **Clear is REJECTED** (§8.1): discarding history is a policy change, and it
could delete the evidence a push never landed.
