# Plan-meter telemetry: reading `/usage` on a cadence, without becoming a budget alarm

**Status:** `PARTIAL(phase 1's instrument, §4.2.1's account identity and §8.3's failure notice BUILT, run by the daemon's tick; §8.1/§8.2/§8.5 open)` —
the instrument is `seneschal/scripts/usage_probe.py` (spawn, parse, failure taxonomy, shape
fingerprint, the account block, one row per attempt) + `seneschal/scripts/usage_activity.py` (the
§10.2 activity snapshot) + `seneschal/scripts/usage_health.py` (§8.3's failure notice), each with its
tests. The daemon half is wired — `presence.py`'s `maybe_usage_reading` (the hourly due-check, last
on the tick) and `maybe_usage_notice` (the notice sender, through the Mouth); `--no-usage-reading` /
`--no-usage-notice` turn them off. **The METER never speaks — §8.2's
conservative branch stands; only the INSTRUMENT may, and only when it breaks (§8.3).** §10 records
what building it measured, including two things this document assumed and one design it did not
contain.

---

## 1. What changed, and why this exists

The position of record used to be that the assistant **could not** read the plan meters — the
`/usage` panel was believed to be an interactive TUI with no headless equivalent. That belief was
wrong, and was disproved by running it:

```
claude -p "/usage"
```

prints the full report as plain text and exits 0 — measured at **~11 s and ~1.2 KB**.

Two things make this live rather than academic. A weekly all-models meter can move twenty-plus points
overnight, inside one weekly window; when the only record is a pair of hand-captured screenshots
seventeen hours apart, *when* it burned has to be reconstructed afterwards from job logs. **That
reconstruction is the cost of under-sampling**, and a series makes it unnecessary.

### 1.1 What this is NOT

**This is not a budget alarm, and it is not Oikonomos.** Oikonomos (`governor.py`, Advisor Chain order
15) meters the daemon's own spend ledger against budgets the owner configures, and gates Fable
delegation. This instrument reads a *different* number — the provider's own published plan meters —
writes it down, and **says nothing about it**. The two count different things (§7 item 7), and neither
may be used as a check on the other.

This spec builds a **meter**. Every design choice below that looks conservative is conservative for
that reason; §5 is where it is argued rather than asserted.

---

## 2. Invocation

### 2.1 Decided: `claude -p "/usage" --output-format json`, argv-built, in a scratch cwd

```
claude -p "/usage" --output-format json --model <cheapest that renders it>
```

spawned with:

| Choice | Value | Why |
|---|---|---|
| shell | **none** — a direct `subprocess` argv list | never a path-converting shell — see §2.2 |
| `cwd` | **a scratch directory outside every repo** | see §2.3 |
| `--mcp-config` | **omitted** | the reading needs no store, no Slack. The daemon's other spawn helpers pass the store MCP by habit; this one must not. Every MCP server threaded in is tool-schema bytes on a request that does no tool work. |
| env | the daemon's child env with `ANTHROPIC_API_KEY` scrubbed | the same billing-safety rule as every other daemon child: the reading must bill the subscription it is measuring, or it measures nothing. |
| `--output-format` | **`json`** | see §2.4 |
| stdout/stderr | **captured**, with `encoding="utf-8"` forced | the CLI emits UTF-8; Windows otherwise decodes it as cp1252 and mangles the em-dash and middot this report is full of. |
| stdin | **`subprocess.DEVNULL`** | see §10.1 |
| timeout | **60 s**, hard | a normal run is ~11 s. A reading that hangs must become a `timeout` row, not a stuck child. |

### 2.2 Why never a path-converting shell — measured, not stylistic

The first attempt failed and **not because of the model**. Git Bash's MSYS path conversion rewrote
the prompt `/usage` into `C:/Program Files/Git/usage`, and the model correctly answered that no such
file exists. **Exit 0, plausible prose, wrong answer.** A mangled invocation does not fail; it executes
something adjacent.

**Binding consequence:** whatever schedules this must not route the prompt through a path-converting
shell. Build the argv as a list in Python and never let a shell see it — the same choice the Watch
peek makes for its prompt, deliberately — rather than relying on `MSYS_NO_PATHCONV=1`. The list form is
the one to use, because it cannot regress.

### 2.3 Why a scratch cwd

`claude` auto-loads the `CLAUDE.md` hierarchy for its working directory. Spawned at the repo root, a
reading pays for the root `CLAUDE.md` it will never consult, on top of the user-level file it cannot
avoid. Spawned in an empty scratch directory it pays only the latter — tens of KB saved per reading
for zero loss of capability. It also keeps a `bypassPermissions` child out of the daemon's working
tree, which is the hazard class `delegated-work-isolation-spec.md` exists for, and here it buys
nothing.

### 2.4 Why `--output-format json`, given that the meters are prose

The JSON envelope's `usage` block is **the probe session's own spend, not the meters**. That was
initially read as a disappointment. It is not: it is **exactly the observer-effect number this
instrument needs.** The envelope gives, free, on every reading:

`is_error` · `duration_ms` · `num_turns` · `total_cost_usd` · `session_id` · `usage`

so every row can carry **what the reading itself cost**, from the provider's own accounting. An
instrument that measures spend and cannot state its own overhead is not trustworthy. The prose is then
parsed out of `.result`.

### 2.5 Model: pin the cheapest tier that renders it correctly

`/usage` renders a report; it does not reason. Pinning a cheap tier keeps the instrument off the meter
it reads. **This was a build-time verification, not an assumption** — and the answer moots the
question (§10.1): the report is rendered **client-side**, no model is called, and the envelope comes
back `num_turns: 0`, `total_cost_usd: 0`. The pin (`usage_probe.DEFAULT_MODEL`) is kept only so it
cannot regress if the CLI ever starts routing this through a model.

### 2.6 Where it runs: a daemon loop check, not `jobs.py`, not a scheduled task

**Decided: a `maybe_usage_reading(...)` on `presence.py`'s main loop, in the exact shape of
`maybe_peek` — an interval knob, a `state/last-usage-reading` stamp file, and a due check.** (Not yet
wired — see the status line.)

**Not `jobs.py`.** The job ledger is for delegated work that produces a result someone is waiting for,
and it **owns a completion push**. An 11-second read that must say nothing (§5) would either fire a
push on every reading — the precise anti-pattern this spec is written to avoid — or need the push
suppressed, which means special-casing the one guarantee `jobs.py` calls non-negotiable. A reading is a
heartbeat, not a job.

**Not a scheduled task.** The daemon has been absorbing heavyweight scheduled runs rather than adding
host-side tasks; a scheduled task is host-side, so no change can create it and it lands in the class of
"merges green and does nothing until someone remembers a manual step"; and it would need its own
health story, logging and failure visibility, all of which the daemon already has.

**The honest cost of the daemon choice:** when the daemon is down there are no readings. Accepted —
`seneschald-health.json` and the updater's Telegram nudge already watch the daemon, and duplicating
that watch inside the thing being watched buys nothing. A gap in the series with a corresponding daemon
gap is legible; §3.3's no-carry-forward rule keeps it that way.

**It must ride the same deferral gates as `maybe_peek`** (`warm_busy`, a headless run in flight, a live
interactive session) — with one difference: a reading deferred past its window is **skipped and
recorded as skipped**, never queued to fire late. A meter reading is only meaningful at the moment it
was taken.

---

## 3. Parsing

### 3.1 The surface

The whole artifact, with illustrative numbers (the reset times name the machine's own zone):

```
You are currently using your subscription to power your Claude Code usage

Current session: 5% used · resets <date>, 3:49pm (<zone>)
Current week (all models): 74% used · resets <date>, 4:59pm (<zone>)
Current week (Fable): 31% used · resets <date>, 4:59pm (<zone>)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 24h · 6694 requests · 294 sessions
  66% of your usage was at >150k context
  49% of your usage was while 4+ sessions ran in parallel
  32% of your usage came from subagent-heavy sessions
  18% of your usage came from sessions active for 8+ hours
  Top subagents: general-purpose 14%, Explore 3%
  Top MCP servers: notion 1%

Last 7d · 32088 requests · 1918 sessions
  76% of your usage was at >150k context
  43% of your usage came from sessions active for 8+ hours
  39% of your usage was while 4+ sessions ran in parallel
  16% of your usage came from subagent-heavy sessions
  Top skills: /assistant 2%
  Top subagents: general-purpose 6%, Explore 1%
  Top MCP servers: notion 2%
```

**The field separator is `U+00B7 MIDDLE DOT`**, space-padded — verified by byte dump, not by eye. The
disclaimer's dash is `U+2014 EM DASH`. **Neither is encoded into the parser** (§3.2, rule 3).

### 3.2 Five parsing rules, each with the observation that forces it

**Rule 1 — key on labels, never on position.** In one capture the 24h block orders its behaviour lines
`>150k / 4+ parallel / subagent-heavy / 8+ hours` and the 7d block orders them `>150k / 8+ hours / 4+
parallel / subagent-heavy`. **The same four facts, two orders, in one document.** A positional parser
is already wrong.

**Rule 2 — every attribution line is optional.** `Top skills:` appears in the 7d block and **not** in
the 24h block. A parser that requires it reports a parse failure on a healthy panel.

**Rule 3 — anchor on the stable keywords, treat the separator as filler.** Match `% used`, `resets`,
`requests`, `sessions` — literal, stable, semantic. A regex containing a literal `·` breaks on the first
release that switches to an en dash, and that break is invisible because the *numbers* are still there.

**Rule 4 — the meter key is DERIVED, never the label.** The interactive panel calls the first meter
**"5-hour limit"**; the headless text calls it **"Current session"**. Same meter, two names. Map to
canonical keys and never store the label as the key:

| Observed label contains | Canonical key |
|---|---|
| `session` or `5-hour` | `session` |
| `week` and `all models` | `week_all_models` |
| `week` and a parenthesised model name | `week_<model_slug>` |
| **anything else** | **not dropped** — appended verbatim to `unrecognised_meters[]` |

**An unknown meter is a finding, not a discard.** If a third weekly pool appears, the row that first
sees it must say so, or the instrument silently under-reports forever.

**Rule 5 — keep the behaviour phrase verbatim as its own key.** Do not normalise `"was at >150k
context"` into an enum. The shipped key is the whole line minus the leading percentage — `"of your
usage was at >150k context"`, prefix included — because stripping a fixed prefix *is* a normalisation
and can break exactly like an enum can. New behaviours will appear; a phrase-keyed dict absorbs them,
an enum drops them without a sound. The phrases are the provider's own category labels about this
machine — not conversation content — and they go only to gitignored `state/` (§6.3).

### 3.3 The failure taxonomy — a parse failure records a parse failure

**This is the hard requirement.** A Watch peek that dies and a peek that finds nothing are
indistinguishable; **this instrument must not repeat that.**

| `outcome` | Detected by | What the row carries |
|---|---|---|
| `ok` | exit 0, **all three** expected meters matched | every field |
| `partial` | exit 0, ≥1 but not all expected meters matched | what matched, plus `unmatched[]`, plus bounded `raw` |
| `unparsed` | exit 0, **zero** meters matched | **no numeric fields at all**, plus bounded `raw` |
| `auth_failed` | recognised auth signature in output or stderr | outcome + signature; an outage is legible **as an outage** |
| `spawn_failed` | non-zero exit, or the child never started | outcome + exit code + stderr tail |
| `timeout` | wall clock > 60 s | outcome + elapsed |
| `skipped` | deferral gate held past the window (§2.6) | outcome + which gate |

**Four invariants, and none of them is optional:**

1. **Every attempt writes exactly one row.** There is no code path that tries a reading and writes
   nothing. An auth outage that kills every spawn for two hours and leaves no trace is an absence
   indistinguishable from a quiet morning.
2. **A non-`ok` row carries no numeric meter fields at all** — not `0`, not `null` beside a populated
   neighbour. **Absent**, so a consumer that assumes presence raises instead of averaging a zero into a
   burn rate (`usage_probe.assert_no_meter_fields`). A zero here is a lie that survives arithmetic.
3. **Never carry forward.** No interpolation, no last-known-good fill, no "same as previous". A gap in
   the series is a gap, and it is *information*.
4. **On `partial` / `unparsed`, retain the raw text**, bounded at 4 KB (the whole report is ~1.2 KB).
   A wording change must be diagnosable after the fact rather than re-derived from a second outage.

### 3.4 How a wording change is detected rather than silently absorbed

Rules 1–5 make the parser *tolerant*. Tolerance is exactly how a change gets absorbed without anyone
noticing. So tolerance is paired with an explicit detector:

- Every row carries **`parser_version`** (an integer the author bumps) and **`cli_version`** (from
  `claude --version`). A parse break correlates with a CLI upgrade, and without the version on the row
  you cannot tell a release from a fluke.
- Every row carries **`shape_fingerprint`**: a short stable hash of the report's *structure* and not
  its numbers — the sorted set of matched line kinds plus the verbatim meter labels plus the verbatim
  behaviour phrases, with all digits stripped.
- **A fingerprint change on an otherwise-`ok` row is a finding.** That is the early warning: the panel
  changed wording, the parse still worked *this time*, and someone should look before the next change
  breaks it.

Without the fingerprint, "it still parses" and "nothing changed" are the same observation. With it,
they are two.

---

## 4. Where it lands

### 4.1 Decided: machine readings get their own file

| File | Writer | Content |
|---|---|---|
| `state/plan-usage.jsonl` | **this instrument, only** | one machine reading per row, one schema, append-only |
| a hand-kept observations file, if the owner keeps one | **hand-authored only** | screenshots, findings, corrections |

Machine readings and human findings differ in **every** dimension that matters to a consumer:

| | machine readings | findings |
|---|---|---|
| writer | one program | a person / a turn |
| rate | ~24/day | ~1/week |
| schema | fixed, versioned | free-form, `kind`-tagged |
| trust | mechanical, verifiable | interpretive, arguable |
| query | *"weekly meter at T?"* | *"why were we wrong?"* |

Merging them means thousands of machine rows a year burying a handful of findings, and no consumer
able to read either cleanly. **Cross-linking, not merging:** a machine row carries no prose, and a
finding may cite machine rows by their `at` timestamp.

### 4.2 The row schema falls out of the questions, and ships whole

`docs/CLAUDE.md` is binding here: **a measurement phase ships whole, never a field at a time.** So the
questions come first, and every field below exists because one of them needs it.

**What a later phase must be able to ask:**

- **Q1** What was each meter at time T?
- **Q2** What was the burn rate over [T₁,T₂], and did that interval cross a reset?
- **Q3** Which weekly window does a reading belong to? *(grouping must not require re-deriving)*
- **Q4** Was the job fleet running during a burn?
- **Q5** Did a scheduled reset actually happen?
- **Q6** Was the instrument healthy at time T?
- **Q7** What did the instrument itself cost?
- **Q8** Did the panel's wording change?
- **Q9** What is Fable's separate meter doing? *(it has its own pool and its own reset)*
- **Q10** Did a new meter or a new behaviour category appear?

**The row** (illustrative values; `at` and `resets_at` are owner-local with an offset):

```jsonc
{
  "schema": "seneschal.plan-usage/1",
  "at": "2026-01-05T12:00:11-05:00",     // Q1
  "outcome": "ok",                        // Q6 — §3.3
  "meters": {                             // Q1,Q9 — absent entirely when not ok
    "session":         {"pct": 5,  "resets_at": "2026-01-05T15:49:00-05:00"},
    "week_all_models": {"pct": 74, "resets_at": "2026-01-09T16:59:00-05:00"},
    "week_fable":      {"pct": 31, "resets_at": "2026-01-09T16:59:00-05:00"}
  },
  "week_window_id": "2026-01-09T16:59:00-05:00",  // Q3 — the window's reset instant, as its id
  "unrecognised_meters": [],              // Q10 — rule 4
  "last_24h": {                           // Q4
    "requests": 6694, "sessions": 294,
    "behaviors": {"of your usage was at >150k context": 66, "...": 49},   // rule 5, verbatim keys
    "top": {"subagents": {"general-purpose": 14, "Explore": 3},
            "mcp_servers": {"notion": 1}}                   // "skills" absent → key absent, rule 2
  },
  "last_7d": { "...": "same shape" },     // Q4
  "probe": {                              // Q7 — §2.4, the observer effect, named
    "cost_usd": 0.0, "duration_ms": 11206, "num_turns": 0,
    "is_error": false, "model": "…", "session_id": "…"
  },
  "parser_version": 1,                    // Q8 — §3.4
  "cli_version": "…",                     // Q8
  "shape_fingerprint": "…"                // Q8
}
```

**Phase 1 adds two blocks this schema did not contain — `interval` and `activity` — and §10.2 is why.**
The row above answers *what the meter read*; it cannot answer *what burned it*. The field contract for
both lives in `usage_activity.py`'s module docstring.

Q2 and Q5 need no extra field: both are answered by differencing consecutive rows within a
`week_window_id`, and a reset is a `week_window_id` change. Storing the window id rather than deriving
it at read time is deliberate — it is the field that stops the mistake of comparing two readings from
**different weekly windows** and calling the difference a burn rate.

### 4.2.1 Q11 — *whose* meter is this?

**The question §4.2 did not ask, and the one that can break the file.** An owner who runs one
subscription to its weekly limit and switches the CLI onto a second one leaves `plan-usage.jsonl`
interleaving **two meters with two different weekly resets** — and **every burn rate or interval
delta computed across the switch is arithmetic on unrelated quantities**, the exact failure
`week_window_id` exists to prevent, one level up. `week_window_id` cannot catch it: two subscriptions
can share a reset instant, and even when they don't, a reader cannot tell a window rollover from a
different account's window. The identity has to be on the row.

**The block, on EVERY outcome** — `ok`, `partial`, `unparsed`, `auth_failed`, `spawn_failed`,
`timeout`, `skipped`, without exception:

```jsonc
"account": {
  "known": true,
  "account_uuid": "…", "email": "…", "organization_uuid": "…",
  "billing_type": "…", "subscription_created_at": "…", "account_created_at": "…",
  "profile_fetched_at": "…",          // when the CACHED profile below was last refreshed
  "label": "main",                     // only when state/accounts.json names this uuid
  "credential_fingerprint": "…",       // 12 hex chars of SHA-256 over the live OAuth token
  "source": "~/.claude.json", "credential_source": "~/.claude/.credentials.json",
  "changed": true, "previous_account_uuid": "…"   // only across a real switch
}
```

Seven decisions, each of which a later edit will be tempted to undo:

1. **It is not a meter field.** Invariant 2 withholds *numbers* from a failed row; whose subscription
   failed to be read is not something the failure puts in doubt. `assert_no_meter_fields()` keeps
   passing with `account` present, and a test asserts both halves.
2. **Two independent signals, because they can disagree.** `oauthAccount` in `~/.claude.json` is a
   **cached profile blob** carrying its own `profileFetchedAt`; `.credentials.json` is what the CLI
   actually authenticates with. A fingerprint that moves while the uuid does not is the discrepancy
   worth catching. §10.5 is what could and could not be established about which of the two leads.
3. **The fingerprint is a hash and nothing else** — the first 12 hex chars of a SHA-256 over
   `claudeAiOauth.accessToken`. Never the token, and **never a prefix of the token**: a truncated
   secret is still a secret, where a truncated hash is not. Only `claudeAiOauth` is hashed; the
   `mcpOAuth` tokens in the same file are other services' secrets and say nothing about billing.
4. **The label comes from an optional local map**, `state/accounts.json` (`seneschal.accounts/1`,
   gitignored). Absent, unreadable, wrong-schema and unmapped all degrade to *no label* and **never to
   an error** — this is a convenience for a human reading the series, and an instrument that breaks
   over its own garnish is worse than one with no garnish. Only `label` is taken: the map's `email` is
   hand-typed, the profile's is measured.
5. **Unknown is a value, and it is never filled in.** An unreadable identity is `{"known": false,
   "detail": "<why>"}` and the row still writes. Nothing carries forward the previous row's account —
   that is invariant 3, and it is the whole reason this field can be trusted at the one moment it
   matters.
6. **A change is a first-class field.** `changed: true` + `previous_account_uuid` when both sides name
   a uuid and they differ; `first_reading: true` on the file's first row, which is not a switch;
   `previous_account_known: false` when the previous row has no readable identity — so a row from
   before the field existed reads as UNKNOWN, never as *"the main account"*.
   `usage_probe.account_changed(prev, cur)` is the pure helper any consumer can call. **A reader that
   computes a delta across a `changed: true` boundary is comparing two different meters.** The interval
   must be **split** at the boundary, never spanned.
7. **The schema does not move and no old row is rewritten.** Still `seneschal.plan-usage/1`; the field
   is additive and optional, `PARSER_VERSION` is untouched (the parsing rules did not change), and
   there is **no backfill** — a row with no `account` never acquires one.

**What this does NOT do.** It adds no cockpit panel, and it speaks no more than the rest of this
instrument does: nothing here imports or reaches a send path.

### 4.3 Peek cost, if a cheap Watch-peek change is ever taken

Out of scope, and named so it is not orphaned: giving `maybe_peek` `--output-format json` and
capturing stdout would price every peek exactly and make a failed peek distinguishable from a silent
one. If that is built, it wants its **own** file with its **own** schema (e.g. `state/peek-metrics.jsonl`)
for exactly the reasons in §4.1. It is not a plan-usage row; it measures a different thing at a much
higher rate.

### 4.4 Retention: keep everything

Do not prune. Compare `presence.db`'s nightly age prune: that is a high-volume edge log whose old rows
answer nothing. This series is the opposite — its **entire value is longitudinal.** A weekly meter is
only interpretable inside its window, so pruning by age deletes precisely the historical burn-rate
comparison the instrument exists to enable. §10.4 has the measured size; revisit only if the file
reaches tens of MB.

---

## 5. What it says to the owner: by default, nothing at all

**Decided: the reading is write-only. A reading produces a row and no message. Ever.**

- **A turn that tells the owner nothing they do not already know produces the WRITE and no message**
  (`../modes/chat.md`'s standing rule). A daily *"usage is fine"* ping is the textbook instance of the
  anti-pattern.
- The owner reads the real panel themselves; the provider publishes it.

**The cure for "I was surprised by an overnight jump" is the recorded series, not a push at 3 a.m.** A
series lets the owner ask *"what happened overnight"* and get an answer with a shape. A threshold alert
would tell them a number they could already see, at a moment they were asleep.

### 5.1 The one exception, and it is not about spend

If anything speaks unprompted, it is **instrument failure**, not usage level:

> *N consecutive readings failed to parse / to authenticate — the plan-meter series has a gap since T.*

This passes the substance test in a way a usage alert cannot: it is information the owner **could not
have had otherwise** (nothing else watches this), it is about a broken tool rather than about their
behaviour, and it carries no number about them. It is the same argument that justifies the updater's
blocked-too-long nudge. It was an explicit decision (§8.3) because it opens a new outbound lane; the
reading itself is still write-only on every path.

### 5.2 Explicitly refused

**No threshold nudge on any plan-meter percentage.** Not at 80%, not at 90%, not "at this rate you'll
exhaust the weekly on Saturday." A rate projection is a budget alarm with better manners. If the owner
wants budget alerts, Oikonomos's configurable rails are where they live — on the daemon's own ledger,
with thresholds the owner sets — not here.

---

## 6. The cockpit

### 6.1 Recommendation: yes, minimally — a live meter

A read-only display of a number the provider publishes is **a live meter by definition**. Recommended
shape, deliberately thin:

- the three current meter values with their reset times;
- the series since the current window opened, as a plain line;
- a visible marker wherever the series has a **gap** (§3.3 invariant 3 makes gaps real, and a chart
  that smooths over them undoes that);
- **no thresholds, no severity colours, no projection, no "remaining" framing.**

The last bullet is the one a future edit will want to violate. It should not.

### 6.2 Placement binds on one precedent

`session-trace-spec.md` §10: anything that re-places a panel like the Trace panel **queries the
container, never the window**. That constraint applies to this display wherever it lands.

### 6.3 Privacy note

The report is the provider's own accounting about this machine. It names no correspondent and quotes
no message. It **does** name subagent types, skills and MCP servers, a bounded `raw` blob is retained on
parse failure, and since §4.2.1 each row carries an **account identity**: a uuid, an email address, an
organization uuid and a credential fingerprint. **No account uuid, email or fingerprint may appear in a
tracked file, a commit message, a test fixture, the RAG corpus or a PR body** — the tests use
obviously-fake values (`00000000-0000-4000-8000-…`, `example.com`) for exactly that reason. The label
map `state/accounts.json` is gitignored like everything else here. The RAG exclusion matters in its
own right: a high-rate daemon artifact fed to the local index drowns it, so this series stays out.

---

## 7. What this instrument cannot do

Stated so a future reader does not over-trust the series. **The first three are the panel's own
disclaimer.**

1. **It cannot see claude.ai.** Web usage is invisible and still counts against the same meters.
2. **It cannot see other devices.** The attribution block is this machine only; the *meters* are the
   whole account. The meters and the attribution have **different scopes in the same report**, and a
   difference between them is not a discrepancy.
3. **The behaviour percentages do not sum and are not a breakdown.** They are overlapping independent
   characteristics — 66% + 49% + 32% + 18% is not 165% of anything.
4. **It cannot attribute spend to a job, a session, or a peek.** `Top skills: /assistant 2%` covers the
   whole skill. There is no per-session attribution at any granularity, and none of §4.2's fields
   invents one.
5. **The 24h/7d request and session counts are rolling and NON-MONOTONIC** — two readings twenty
   minutes apart can show the count *falling* as the trailing edge of the window moves. **Never
   difference these to derive a rate.** Only the meter percentages are cumulative, and only within a
   window.
6. **The percentages are integers.** One-point granularity bounds every derived quantity.
7. **It measures the plan, not the cost basis.** It cannot corroborate or refute
   `governor-ledger.jsonl`'s metering; the two count different things, and treating one as a check on
   the other is exactly the flat-sum mistake the billable basis fixed.
8. **It is not free.** Every reading is a real spawn against the meter it reads. §4.2's `probe` block
   exists so that overhead is always visible in the data rather than argued about.

---

## 8. Decisions — options, with a recommendation, for the owner

### 8.1 Cadence — OPEN; phase 1 is built on the recommendation

| | Option | Cost |
|---|---|---|
| **A ✅ recommended** | **Hourly, plus a boundary pair: one reading ~5 min before each weekly reset and one ~5 min after.** | ~26 spawns/day. |
| B | Every 15 min | ~96/day. Resolves well under the meter's own 1-point granularity — buys resolution the data does not have. |
| C | Every 5 h, aligned to the session window | ~5/day. Cheapest, and a twenty-point move lands in one unresolvable gap. |
| D | Twice daily | Strictly worse than a daily screenshot. |

**Why hourly.** Under peak load the weekly meter moves on the order of a point an hour — about its own
1-point granularity. So an hourly reading resolves the weekly meter to the finest step it can express,
and no finer cadence can add information. It also yields ~5 readings inside each 5-hour session
window, enough to characterise a window rather than merely sample it.

**The boundary pair is not an optimisation.** Without a **pre-reset** reading, a week's final total is
never observed — the series just stops mid-week and resumes near zero. Without a **post-reset** reading,
a reset is indistinguishable from an instrument failure. Together they make §4.2's Q5 answerable.

**Both failure modes, named:**

- **Under-sampling costs attribution.** You learn that twenty points burned and not when, so *when*
  has to be reconstructed from job logs by hand.
- **Over-sampling costs the measurement itself.** Each reading is a spawn that increments the very
  `sessions` counter the report publishes. At a 5-minute cadence the instrument would add ~288
  sessions/day to a panel that may be reporting a few hundred — **roughly doubling the number it
  exists to observe.** This is not a cost-of-tokens argument; it is contamination.

### 8.2 Should anything ever speak about usage *level*? — OPEN; built on A

| | Option | Note |
|---|---|---|
| **A ✅ recommended** | **Never. Silence on level, unconditionally.** | §5. The owner reads the panel; the series answers "what happened". |
| B | One message per weekly window if the meter crosses a high mark | Honest case **for**: an overnight jump can genuinely surprise. Honest case **against**: it tells the owner a number they can already see, at a time they are not looking — and once a threshold exists, its number becomes a thing to argue about. |
| C | A rate projection | Refused in §5.2 and not recommended in any form. |

### 8.3 Should instrument *failure* speak? — **DECIDED: A**

The decision was *"failure should speak — but nothing crazy."* Built as
`seneschal/scripts/usage_health.py`, sent by `presence.maybe_usage_notice` once that is wired.

| | Option | Note |
|---|---|---|
| **A ✅ DECIDED** | **Yes — after N consecutive failures, one message, once per outage.** | Passes the substance test: about a broken tool, carries no number about the owner, and nothing else watches this. |
| B | ~~Never; the gap is visible in the data~~ | *Rejected.* An instrument that quietly stops working stays quietly stopped. |
| C | ~~Surface it only in the cockpit~~ | *Not chosen.* Available as an addition, not a substitute, since it requires opening the surface. |

#### 8.3.1 The parameters, and what *"nothing crazy"* was read as

Each is one edit to change.

1. **N = 3 consecutive non-`ok` readings** (`usage_health.FAILURE_STREAK`) — ~3 h of blindness at the
   hourly cadence. **A one-off stays silent**, so *"a missed reading is a missing row, not an
   incident"* survives.
2. **One message per failure EPISODE.** After it fires it does not fire again for that unbroken run:
   no ladder, no re-nag, no escalation at hour six. This is the whole content of *"nothing crazy"*.
3. **One message on recovery — and only if the break was announced.** A break nobody was told about
   recovers in silence. **Together, 2 and 3 cap an outage at exactly two messages, ever.**
4. **It respects the daemon's existing quiet window and NEVER pierces it.** A dead instrument at 03:00
   is not a Critical reminder. The gates are `sentinel.is_quiet` (the owner's explicit do-not-disturb)
   and `sentinel`'s night curfew on the owner-local clock — **imported, never a second copy** of *"is
   this a reasonable hour"*. A notice due inside the window is not sent and nothing is stamped, so the
   next reading re-asks and it goes out when the window opens. If the hour cannot be determined, it
   fails toward speaking.
5. **The message says what is broken, since when, how many attempts — and nothing else.** No meter
   number (that is §8.2 leaking in), no diagnosis, no instruction. It carries the `outcome` name, the
   two timestamps, the count, and the log path.
6. **The episode state is DERIVED from the row stream**, not kept in a sidecar: the consecutive count
   comes from walking `plan-usage.jsonl` backwards to the last success, every time. The one thing the
   rows cannot carry is *"we already told them"*, so **exactly one persisted flag** exists — a `notice`
   block inside the existing `state/last-usage-reading`, stamped only after a landed send. It is keyed
   by the episode's own derived `started_at`, which is what makes a restart unable to re-announce a
   live episode or resurrect a resolved one.

**One departure from a literal reading of *"non-`ok`"*, and it is deliberate: a `skipped` row is
TRANSPARENT** — it neither counts toward a run nor breaks one. A `skipped` row is the daemon
*declining* to read because a warm turn, a headless child or a live session held the deferral gate;
the instrument is fine. Three of those in a row is an ordinary working afternoon, so counting them
would send *"the instrument is broken"* mid-session — the false alarm that makes a real one
unreadable. Transparent rather than run-breaking is the conservative half of the same choice: a skip
does not prove the instrument works either, so it may not silently reset a genuine break.

**`partial` DOES count as a failure.** It is a degraded parse, and a degraded parse that persists is
exactly the wording change §3.4 exists to surface. The `partial` rows still carry their meters, so the
series is not blind — but the message about them still names no number.

### 8.4 `auth_failed` is a different animal — **OPEN**

An expired OAuth refresh token stops **every** new `claude` session from starting — every job, the
analyst, `fable_delegate`, every comms peek — and, absent this instrument, nothing records it; it is
reconstructed afterwards from an absence. An hourly prober is structurally the **first thing that
would notice**, which makes an `auth_failed` row a canary for the **entire spawn path** rather than a
fact about one instrument. A `timeout` or an `unparsed` says *this reading did not land*; an
`auth_failed` says *nothing can spawn right now*, and those are not the same news.

**The open question:** should `auth_failed` bypass the 3-strike threshold?

| | Option | Note |
|---|---|---|
| A | Leave it inside §8.3's 3-strike rule (what is built) | Consistent and quiet; costs ~3 h before the one thing that noticed a total outage says anything. Under-reacts to the case with the widest blast radius. |
| B | `auth_failed` speaks at N=1, everything else at N=3 | Fastest; a single `auth_failed` is already high-confidence — a recognised signature, not an inference. Costs a message on a transient auth blip, which has not been measured. |
| C | `auth_failed` speaks at N=2 | ~1 h. One repeat is cheap evidence that it is not a blip. |
| D | Escalate it out of this spec entirely — a spawn-path health check that watches every child | The largest and most correct-sounding, and the one most likely to stay unbuilt. Named so it is not smuggled in as a rationale for doing nothing here. |

**Not built, and deliberately not pre-empted by §8.3.** The failure set is one tuple
(`usage_health.FAILURE_OUTCOMES`) and the threshold is one constant, so B or C is a small edit once
decided.

---

## 9. Phase plan

**Phase 1 — the whole instrument, in one change. BUILT as modules; daemon wiring pending.**
Invocation (§2), parser + failure taxonomy + shape fingerprint (§3), `state/plan-usage.jsonl` with the
complete §4.2 row, the activity snapshot (§10.2), the account block (§4.2.1), unit tests over the
verbatim §3.1 sample **plus** a mutated copy per §3.2 rule (reordered lines, missing `Top skills`, an
unknown meter label, a swapped separator) and one per §3.3 outcome. No message on any path. There is no
phase 1a: a field added later cannot be backfilled — the window it would have described has already
reset.

**§8.3 — the instrument-failure notice. BUILT as a module; the sender is part of the pending daemon
wiring.** `usage_health.py` + its tests are the whole of that decision and nothing else — it touches
neither the cadence nor what the level does.

**Phase 2 — reading it.** Whatever §8.1, §8.2 and §8.4 decide: the cockpit display (§6), any speaking
about LEVEL (§8.2), and whether `auth_failed` bypasses the threshold (§8.4). Gated on decisions, not
on data.

**Not in this spec:** the Watch peek's `--output-format` change (§4.3), and any change to what the
Watch peek escalates.

---

## 10. What building it measured

Three things in the plan above were assumptions, and one thing the plan needed was not in it at all.
Recorded here rather than silently absorbed, so a later reader can see which claims were checked.

### 10.1 `/usage` is rendered CLIENT-SIDE — and this does NOT weaken §8.1

The build-time probe (§2.5) returned, from the provider's own envelope: `num_turns: 0`,
`total_cost_usd: 0`, `duration_api_ms: 0`, `modelUsage: {}`, an all-zero `usage` block, and a report
byte-identical in structure on a Haiku pin. No model is called.

**The tempting wrong conclusion is that the instrument is free and the cadence could be much faster.**
It is not, and §8.1's argument survives intact — because that argument was never about tokens. A
zero-token reading still costs **one session and its requests**, and the contamination §8.1 argues
about IS the session count. A future edit reaching for a finer cadence on the strength of §10.1 has
misread it.

Two smaller invocation facts, both load-bearing in `usage_probe.py`:

- **`stdin` must be `subprocess.DEVNULL`.** Without it the CLI waits 3 s for piped input and warns on
  stderr. A reading that spends 3 s of its budget waiting on a pipe nobody is writing to is a slow
  reading for no reason.
- The separator is confirmed **U+00B7 MIDDLE DOT** and the disclaimer's dash **U+2014 EM DASH**, by
  byte dump on a second independent capture. Neither appears in any pattern — §3.2 rule 3 held.

### 10.2 The activity snapshot: the requirement this spec did not cover

The ask was a periodic `/usage` log *alongside what was happening in that time period, so it can be
analysed in depth*. The plan above builds the first half in full. **A reading of "74%" is nearly
useless on its own.** *"74%, and in the hour before it there were 3 jobs running, 12 warm turns, 11
comms peeks, the warm session was at 170k context, and the daemon did not reload"* is an analysable
row.

So every row carries **`interval`** (previous reading → this one, stated explicitly) and **`activity`**
(a snapshot over that window). Design rules, each of which is the honesty of the thing:

1. **Every field comes from a source that already exists.** `state/jobs/*.json` via `jobs.list_jobs`,
   `metrics.jsonl`, `governor-ledger.jsonl`, `presence.log`, and the daemon's own process start.
   Nothing was instrumented, nothing new is written, and `usage_activity.collect` never raises — a
   snapshot is an addition to a reading, never a reason to lose one.
2. **The window is on the row.** Without it a gap — daemon down, auth outage, machine asleep — is
   silently attributed to whatever period the reader assumes. `interval.gap` flags a window materially
   longer than nominal and **does not explain it**, because nothing here knows why.
3. **A first-ever reading carries no activity at all**, rather than a window invented from the nominal
   cadence. An assumed window is a fabricated measurement.
4. **A count is only reported when the scan covered the window.** Each file is read as a bounded
   growing tail; a scan that did not reach `since` sets `scan_truncated` and the number becomes a
   stated lower bound. A silently under-counted hour reads exactly like a quiet one.
5. **Peek cost stays `null`.** A peek writes to neither ledger, so the *count* is real and the *cost*
   is unknown. It is not estimated into a number that would read as measured a month later (§4.3 is
   the fix, out of scope).
6. **A source that cannot be read yields `error` and no counts.** Never a zero — the same rule as §3.3
   invariant 2, for the same reason.

Measured cost of the snapshot on a busy tree: **well under 100 ms and ~2 KB** for a one-hour window.

### 10.3 Which branch phase 1 took on each open decision

None of §8's decisions was made when phase 1 was built, and it took the conservative branch of each,
so nothing foreclosed a decision and every one remains a change to make rather than a change to undo.

| Decision | Branch taken | Effect |
|---|---|---|
| §8.1 cadence | **A**, the recommendation | Hourly + the boundary pair, as `--usage-interval-min` (default 60, one named constant) and `--usage-defer-grace-min` — flags on the pending daemon wiring. Changing it is a flag, not a rewrite |
| §8.2 does level ever speak | **A**, never | Nothing in the reading imports a send path; a test asserts it against the import graph. B or C would be new code |
| §8.3 does instrument failure speak | **A**, decided | `usage_health.py` announces a 3-reading break once and its recovery once, inside the quiet window's rules. §8.3.1 is the parameters |

§6's cockpit display is likewise unbuilt: it is phase 2.

### 10.4 The row is bigger than first estimated

§4 first sized a row at ~200 B. With the activity snapshot and verbatim behaviour keys a row is
**~2.2 KB**, so ~26 readings/day is **~21 MB/year**. **The retention decision does not change: keep
everything.** The reasoning in §4.4 is about what the series is *for*; a few years to reach the revisit
threshold is a note in the calendar, not a reason to start throwing away the answer.

### 10.5 Which file leads on an account switch — partly established, the rest NOT determined

§4.2.1 rule 2 rests on the claim that the two identity signals can disagree. What an account switch
lets a host establish, and where the evidence runs out:

**What can be established.** From `~/.claude.json` → `oauthAccount` and `~/.claude/.credentials.json`
→ `claudeAiOauth`, read after a switch: `subscriptionCreatedAt` and `profileFetchedAt` are durable
content timestamps, and the access token's mint time is recoverable as `expiresAt` minus its TTL (an
8-hour TTL lands `expiresAt − profileFetchedAt` on a round number to within tens of milliseconds). The
token is minted **just before** the profile is fetched — the only causally possible order, since
fetching a profile requires authenticating as it. **So the credential material precedes the profile
content**, and the profile blob is demonstrably a **cache**: it carries a fetch timestamp precisely
because it is not re-read.

**What is NOT determined: which FILE's bytes hit the disk first.** Two reasons, both structural:

- **mtime is last-write-only.** `.credentials.json`'s mtime can trail the token it contains by tens of
  seconds, equally explained by a post-login refresh rewriting the file, so it cannot testify about
  the switch — only about the most recent write.
- **The decisive backups roll off.** `~/.claude/backups/` keeps a short ring of `.claude.json`
  backups; by the time anyone looks, all of them may already carry the new account.

**Stating that plainly rather than asserting a lead is the point.** The claim rule 2 needs is weaker
and is established: the profile is a cache with its own fetch stamp, so a row can name an account the
credential beside it has already left. Both signals are logged because the ordering of their *writes*
is unknown, not in spite of it. If it is ever worth settling, the measurement is cheap and must be
taken **live**: watch both files' mtimes across a deliberate `/login`, before the backup ring rotates.

## Router entry

**What it decides:** reading the real plan meters on a cadence **without becoming a budget alarm**.
`/usage` runs headless; it is spawned as an argv list (never a path-converting shell) in a scratch cwd;
the parser is label-keyed, the meter key derived, every attempt writes exactly one row, a non-`ok` row
carries no numbers, and nothing is carried forward. Every row names the account it was read under.
The reading never speaks; the instrument speaks only when it breaks (§8.3), capped at two messages per
outage and never through the quiet window. The daemon loop checks are the remaining wiring.
