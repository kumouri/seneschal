# Job `origin` and the fresh-eyes return path

**Status:** `PARTIAL(phases 0-3,5 BUILT)` — **phase 0** (`--origin-session` + the
`CLAUDE_CODE_SESSION_ID` auto-stamp + `--goal`, registry enrichment, `jobs.py status` prints it) ·
**phase 1** (`seneschal/scripts/job_analysis.py`: the `seneschal.job-leads/1` schema, its strict
validator — unknown-key rejection, established-before-conjecture ordering, `cited` required on
established, empty list valid — and `build_delegation(artifact_path, goal)`; pure functions) ·
**phase 2** (`jobs.py analyze <id>` + `--analyze` on `start`, both goal-required; the analysis is a
second, **origin-less** job — refused an origin in code — that runs `job_analysis.py run`, validates,
and stages `state/jobs/<id>.analysis.json`) · **phase 3** (the §3.3 ladder: rung 2 = `wake_text` gains
the artifact path, rung 3 = `state/session-mail/<session_id>.jsonl` + `jobs.py mail` + the orientation
read, rung 4 = untouched and pinned by test; plus the Dream sweep `prune --mail-days`) · **phase 5**
(`origin.request`, WHO ASKED — §3.5). §11.1: the analysis job picks the host repo as its `cwd`
explicitly rather than inheriting a worktree that has already been torn down. **Phase 4 (a dedicated
analyst archon) is unbuilt** — `job_analysis.ANALYSTS` is the one-line seam, and an unknown analyst is
refused loudly rather than silently downgraded to `warm`. · **Scope:** `seneschal/scripts/jobs.py`,
`seneschal/scripts/job_analysis.py`, one touchpoint in `presence.py`'s job reconcile, and the
orientation read in the `/assistant` command (`.claude/commands/assistant.md`). **Parent:**
`background-jobs-spec.md` — read that first; this is its next chapter.

**Decisions on §9 (build to these):** (1) analysis is **opt-in per job**, never automatic; (2) mailbox
retention is **14 days**, one named constant (`jobs.RETENTION_DAYS`); (3) **`--goal` is mandatory when
analysis is requested** — refused in code, because an analysis with no goal line is an analyst
guessing what "working" would have looked like.

**The problem.** A `--wake` job hands its artifact to an analyst who has no sunk cost in the working
agent's hypothesis, and that analyst sees the anomaly instead of explaining it away. **But the analysis
lands in the owner's Telegram, not back at the session that asked for the work**, so the owner relays
it by hand.

---

## 1. What was true before this

| Fact | Where |
|---|---|
| `start_job()` accepted `origin: dict \| None = None` | `jobs.start_job` |
| …and stored `"origin": origin or {}` in the record | `jobs.start_job` |
| The **only** production call of `start_job` passed `title/argv/cwd/channel/wake/lease/deadline_sec/retry` — **and no `origin`** | `jobs.py` `start` handler |
| No `--origin*` flag existed on `jobs.py start` | the `start` flag block |
| **Nothing anywhere read `origin`** | a repo-wide grep |
| Every job record carried a literal `"origin": {}` | `state/jobs/` |
| The completion push fires off the **record**, unconditionally, once per terminal state | `jobs.reconcile` |
| `--wake` additionally enqueues a synthetic inbound, built from the record after the push landed | `jobs.reconcile` → the daemon's job reconcile |
| That enqueue path feeds **only the daemon's warm session** | the daemon's durable action queue |
| The record round-trips unknown keys: `save_job` writes the whole dict, `load_job` requires only `id` | `jobs.save_job` / `jobs.load_job` |
| Record schema is `seneschal.job/1` | `jobs.py` |

So the return-address slot existed, was inert, and had never carried a byte. That is the cheapest kind
of feature to add: the field is already in the schema, in every record on disk, and in every reader's
tolerance.

### 1.1 The load-bearing fact

**There is no inbound channel to a session that is not the daemon's warm session.**

`--wake` looks like a general "tell the session" mechanism. It is not. It builds a synthetic inbound
line (`jobs.wake_text`) and hands it to the daemon's enqueue path, which appends to the daemon's own
durable action queue and wakes its drainer. A desktop `/assistant` chat and a hook-stamped `build`
session have **no queue, no drainer, and no socket**. Nothing can be pushed at them. They can only
*read*.

That single fact decides most of this design: routing to the originating session is a **filing**
problem plus a **pull** problem, not a delivery problem, and any spec that treats it as delivery will
specify something that cannot be built.

---

## 2. The problem, precisely

Two halves, in the same shape as the parent spec's §1.

**(a) The answer has no address.** The analysis is genuinely useful *because* the analyst is ignorant
of the working agent's reasoning — it sees the anomaly instead of narrating around it. But it is
delivered to the one channel `jobs.py` knows: the owner's Telegram. The owner reads a lead list about
a session they weren't in, then re-types it into the session that needed it. The mechanism works and
the routing is a human.

**(b) The address that exists is never written.** `origin` had been in the schema since the module
shipped and was empty in every record. It was not a broken feature — it was an unstarted one, and it
is *exactly* the shape of thing that stays unstarted forever unless something writes it by default.
Compare `state/metrics.jsonl`, whose contract lived prompt-side only and produced zero rows in a month.
**A field a caller must remember to populate is not a mechanism.** That precedent drives §3.1's
recommendation more than any ergonomic argument does.

---

## 3. The addressing design

### 3.1 Where the address comes from — auto-stamp from the environment, primary

The candidates were "a `--origin-session <id>` flag, **and/or** auto-stamping from the session
registry." Reading the registry says: **auto-stamp, but from the environment, not from the registry.**
The flag is the override, not the primary.

**Why not the registry.** `seneschal/state/sessions/` knows who is live, but it cannot tell you *which
one is calling you*:

- Every hook-stamped entry carries **`pid: 0`** — `session_stamp.py` sets a fixed marker pid on
  purpose, so `started_at` stays stable across a session's events. Walking up the process tree to find
  your own session's entry therefore matches nothing.
- `cwd` + `branch` is routinely ambiguous: two live sessions in the same checkout on the same branch
  is the normal case with `.claude/worktrees/` in play, not a race.
- The daemon and a desktop `/assistant` don't record a `cwd` at all (`sentinel.write_session_heartbeat`
  stores it only when passed; `session_heartbeat.py` never passes one).

**Why the environment works.** A Claude Code session exports `CLAUDE_CODE_SESSION_ID`, and a child
process inherits it. The value is **byte-identical to the session's registry filename**, because
`session_stamp.py` passes the harness's own `session_id` through to `sentinel._session_id_for`. So the
environment and the registry already agree on the identifier; the environment is simply the copy that
knows *which* one you are.

**The recommendation:**

```
origin.session_id  =  --origin-session <id>            # explicit, wins
                   |  $CLAUDE_CODE_SESSION_ID          # auto, the default path
                   |  (absent)                         # → §3.3 rung 4, unchanged behaviour
```

Auto is primary because of §2(b): a flag nobody passes is `metrics.jsonl` again. `jobs.py start` is
typed by a model mid-turn, and the model that forgets `--wake` will forget `--origin-session`. The env
read costs one line and is right by default for every caller that *is* a session — a warm turn, a
desktop `/assistant`, a `build` session, a scheduled Dream run. The flag exists for the callers that
aren't (a cron shim, a hand-run command) and for tests.

Fail-open, per the module's posture: an unreadable or absent env var means an empty `origin`, which
means the pre-existing behaviour exactly.

### 3.2 What `origin` carries

```json
"origin": {
  "session_id": "00000000-0000-0000-0000-000000000000",
  "source": "build",
  "cwd": "C:/.../seneschal-dev",
  "branch": "docs/job-origin-routing-spec",
  "goal": "find out why the nightly RAG refresh writes zero project docs",
  "stamped_by": "env"
}
```

| Field | Why it's in |
|---|---|
| `session_id` | the address. Everything else is context. |
| `source` | `daemon` / `desktop` / `build` / `scheduled` — decides which rung of §3.3 is even *possible*, and it's the registry's own vocabulary (`sentinel.GATING_SOURCES`). |
| `cwd`, `branch` | enrichment, copied from the registry entry when one exists. Not identity (§3.1) — but a lead list is worth more when the reader knows which checkout it was about, and a job outlives the branch it was started on. |
| `goal` | **the one line** (§4.1). One line, and the cap is enforced at write time. |
| `stamped_by` | `env` / `flag` / `none`. Cheap provenance: it is how you find out, weeks in, whether the auto-stamp is actually firing — the §7.4.1 argument from the parent spec, applied to this feature before it needs it. |

**What deliberately does not go in:**

- **The originating session's transcript, hypothesis, prior attempts, or diagnosis-so-far.** §4 is
  the argument. The structural point is that `origin` is the only new channel between the working
  agent and the analyst, so keeping those out *of the record* means there is no place for them to
  leak from later. A field that doesn't exist cannot be helpfully populated by a future improvement.
- **A callback command.** An `origin` that names a command to run would be a remote-execution
  primitive: the record is written by whoever calls `jobs.py start` and read by the **daemon**, so
  `origin.cmd` lets any job author make the daemon execute arbitrary code as itself. The routing
  needs an address, not a callback.
- **The pid.** `pid: 0` for every hook-stamped session (§3.1), and a pid is not an identity across a
  restart anyway.
- **Anything secret.** No env dump, no tokens. The record is world-readable on the box and its `.log`
  sibling is already the thing that gets quoted into Telegram.

### 3.3 When the originating session is gone — which is the common case

The daemon outlives sessions; the session that made the promise usually does not. A desktop
`/assistant` entry ages out of its gating TTL in **120 s** (`sentinel.SESSION_TTL_SEC`), drops out of
the awareness listing after **1 h** (`SESSION_VISIBLE_TTL_SEC`), and is deleted outright after **24 h**
(`SESSION_PRUNE_SEC`). A `build` session's entry is removed the moment it ends (`session_stamp.py`).
Meanwhile the parent spec notes the warm session dies on idle wind-down, on any turn error, and on every
merge reload. **Design for the address being stale.**

The ladder, evaluated when the job reaches a terminal state. It is a ladder of *additions*: the
Telegram push has already fired by the time any of this runs.

1. **Always: file it.** Write the analysis artifact to `state/jobs/<id>.analysis.json`, next to the
   record and the log. This happens first and unconditionally. Disk is the durable surface; delivery
   is the optional part. If everything below fails, the artifact still exists, `jobs.py status <id>`
   still prints its path, and the completion push still names it.
2. **`origin.source == "daemon"` and the warm session is live** → the existing `--wake` synthetic
   inbound, with the artifact path in the line. Already built; this rung is a one-line change to
   `wake_text`, not a new mechanism.
3. **`origin.source` is `desktop` / `build` / `scheduled`** → there is no push channel (§1.1), so
   append one line to a **pull mailbox**: `state/session-mail/<session_id>.jsonl`. The precedent is
   `state/session-distillations.jsonl`, which the `/assistant` command's grounding block already tells
   every `/assistant` session to tail at orientation. Same shape, same read moment, addressed rather
   than broadcast.
4. **`origin` is empty, unparseable, or names a session with no mailbox reader** → nothing extra
   happens, and that is fine, because the **Telegram completion push already fired** off the record.
   That is the floor, it requires no new code, and it is the pre-existing behaviour in full.

#### 3.3.1 The orientation read

The daemon's half of the orientation read lives in `presence.py`'s grounding block. The `/assistant`
half is one step in `.claude/commands/assistant.md`'s orientation list:

```markdown
Check this session's **mail**: `python seneschal/scripts/jobs.py mail` (act-low; prints "no mail" and
exits 0 when there's none, so it's cheap). This is where a background job's fresh-eyes analysis is
filed *addressed to you* — a ranked list of **places to look**, with the evidence for each, produced
by an analyst that was given only the job's log path and its one-line goal and nothing of whatever
anyone thought had gone wrong. Treat it as leads, never as a diagnosis: check what it points at
before you theorise, and an empty list is a real answer. (It's a pull, not a push: there is no
inbound channel to a desktop session, so mail sits until a session reads it. The owner already got
the completion push either way.)
```

If the step is ever removed, behaviour degrades to rung 4 — the Telegram-push floor — rather than
breaking, which is the whole point of the floor.

**The invariant this ladder must not break:** the parent spec's *no silent path* — every terminal
state notifies exactly once. Routing may only ever **add** a delivery. It must never make the existing
push conditional on a successful route, and it must never move the push. If a future change wants
"don't buzz the owner when the session got it," that is a separate decision with a separate argument;
it is not this feature, and doing it by accident is how the no-silent-path guarantee dies.

**Honest limitation, stated rather than papered over:** rung 3 is a pull, and a pull needs a reader. A
session that closes before its next orientation never sees its mail. That's acceptable *because* rung 4
already happened — the answer reached a human either way. It also means the mailbox must be swept:
`jobs.py prune` (nightly in Dream) grows a `--mail-days` sweep, and per the standing rule for `state/`
files, mailboxes are **append-only JSONL, built-then-`os.replace`d**, never truncate-written.

**And what the reader RECORDS.** A reader that returns lines and writes nothing leaves three gaps:
nobody can tell delivered-and-read from delivered-and-ignored; every read shows everything forever, so
a session that orients twice meets its old mail as new; and **a nightly sweep that grades by AGE
ALONE** quietly deletes mail nobody has read.

So the reader leaves a receipt: **`state/session-mail/<session_id>.cursor.json`**, a per-session
high-water mark holding the last-read entry's id, its timestamp and a `read_at`. Three properties are
load-bearing:

- **It is a separate file, not a stamp inside the mailbox.** Stamping `read_at` onto entries means
  rewriting a file `append_session_mail` may be appending to at that instant — jobs finish whenever
  they finish — and a read-modify-write racing an appender can LOSE AN ENTRY, which is strictly worse
  than the bug being fixed. A cursor is written only by the reader and read only by the reader, so it
  cannot race the appender at all. The mailbox stays append-only and single-writer.
- **A read never fails because a receipt could not be written.** `read_session_mail`'s contract is
  that it "must never be what breaks a session"; a confirmation is strictly less important than the
  mail it confirms. An unwritable cursor costs the receipt and the unread filtering — the mail is
  still returned in full, and the CLI says the receipt did not land rather than implying it did.
- **Unread mail is never age-swept, and the hold is reported.** Age was only ever a proxy for the
  question the cursor now answers directly. Sweeping-and-logging would still destroy the delivery and
  merely narrate it into a nightly step nobody reads line by line. The named cost: a mailbox for a
  session that never returns grows without bound — visible in `prune`'s output, and remediable by a
  human deleting it, which a GC that has already eaten the mail is not.

Entries carry an `id` for the cursor to point at; **an entry that predates ids is data to migrate, not
a fixture to delete**, so entries without an id get a stable content hash instead — sound precisely
because the mailbox is append-only, and requiring no write to the file it sits in. `jobs.py mail` shows
unread by default and advances, `--all` shows everything, `--peek` shows without advancing, and every
read prints `N new, M total` so *"no mail"* and *"nothing new"* stop looking identical.

### 3.4 Backward compatibility

Mechanically already satisfied, and verifiable rather than hoped-for:

- `save_job` writes the whole dict with no key whitelist and `load_job` requires only `id`, so a record
  gaining keys inside `origin` round-trips through every existing path, including the shim and the
  retry respawn.
- `cockpit/server/jobs.py` never reads `origin`. It is a deliberately independent reader of the
  on-disk shape (`cockpit-spec.md`), and an extra populated key changes nothing there — the same
  conclusion the parent spec reached for `attempts[]` (§7.4.1).
- **The schema stays `seneschal.job/1`.** This is an additive key inside a field the schema already
  declares, not a new version. Bumping it would force every reader to care about a change none of them
  can see.
- **The rule for every future reader: `origin` may be `{}` or missing, and those two must be treated
  identically.** Nothing may require `session_id`. A falsy `origin.get("session_id")` is rung 4, which
  is exactly the pre-existing behaviour.

### 3.5 WHO ASKED — `origin.request` (phase 5)

**The problem.** Every job should record who initiated it: the Telegram message *from the owner* that
caused the spawn; for a turn with no message handle, the trace row's turn id; and when the warm session
started the job on its own initiative with no owner message behind it, a record saying so, carrying the
turn where that decision was made.

**This is a phase of this spec, not a spec of its own**, for three reasons and one of them is
structural. It writes into `origin`, whose shape §3.2 enumerates and whose round-trip rules §3.4 sets —
splitting the definition of one field across two documents is how the two get edited separately. Its
argument for auto-stamping is §2(b)'s, unchanged: *a field a caller must remember to populate is not a
mechanism.* And §3.3's ladder is already the *delivery* ladder — where the answer goes; this is the
*requester* ladder — where the question came from. Two ladders, one address block.

#### 3.5.1 Why `origin.session_id` could not already answer this

It answers *which session*, and the question is *who asked*. One warm session serves hundreds of
turns, some of them the owner asking for work and some of them the assistant deciding on its own — and
in practice a single warm session id ends up on dozens of job records with no way to tell which ones
the owner asked for.

Further facts decide the mechanism:

| Fact | Where |
|---|---|
| The environment has **no per-turn variable**. A warm session is one long-lived `claude` process, so `CLAUDE_CODE_SESSION_ID` is fixed for its whole life | `presence.child_env`; the id is set once, at spawn |
| `turn_id` is minted **in the drainer**, per turn, and is the only correlator the transcript protocol has | the daemon's drainer, `uuid.uuid4().hex[:12]`; `state/README.md` → `warm-transcript.jsonl` |
| The durable queue entry is `{channel, text, attempts}` — **it does not carry the Telegram `message_id`** | the daemon's queue entry / state loader, which rebuild exactly those three keys |
| The message id lives **only** in the daemon's in-memory inbound-id map, keyed `message_id -> the exact text as enqueued` | recorded after the force-route transform |
| A **picker tap contributes no id at all**, on purpose: a `callback_query`'s `message_id` names *the assistant's* question, not a message from the owner | `telegram_poll.extract_callback`, and the id filter in the poll loop |
| The warm session's id **is** the id the trace panel keys on — the same id appears in `warm-transcript.jsonl` and as `origin.session_id` on job records | `state/` |

That last row is the join that makes rungs 2 and 3 buildable at all: the id `jobs.py` auto-stamps from
the environment and the id the trace panel groups by are **the same string**, so a `turn_id` recorded
beside it lands on a row the owner can actually click.

#### 3.5.2 The turn pointer — how `jobs.py` learns which turn it is inside

Since neither the environment nor the registry can say (§3.1), the daemon leaves a pointer at the one
moment it knows: **`state/current-turn.json`**, written where `turn_id` is minted and closed in the
same `finally` that closes the interleave window.

```json
{ "schema": "seneschal.turn-pointer/1", "turn_id": "9d41c0a7be12",
  "session_id": "00000000-…", "surface": "telegram", "by": "owner",
  "message_id": "4242", "opened_at": "…Z", "closed_at": null }
```

- **One file, no keying**, because the daemon runs exactly one warm session at a time — the same
  invariant the daemon's session-open bookkeeping already rests on. If that ever stops being true this
  must be replaced (keyed per session), not patched.
- **It carries ids and never text.** No body, no preview, no goal. A `!private` turn has to be exactly
  as safe to stamp as any other, and the cheapest way to guarantee that is for there to be no field a
  future edit could helpfully populate. Asserted as an absence in the tests.
- **`by` reuses the turn log's origin classifier** rather than re-spelling it: the turn log already
  trusts it for precisely this distinction (a meaningful fraction of what arrives on the owner's
  channel is machine-generated), and two spellings of *"did a person write this?"* would drift apart.
- **`message_id` is a uniqueness-proved reverse lookup.** The forward direction (text remembered by
  id) is keyed by id and cannot be ambiguous; the reverse can — two messages reading `ok` produce two
  ids for one string. So a tie answers **nothing**, with the count in the reason, and the caller
  records `unresolved`. This is the one place a wrong attribution could realistically have been
  manufactured, and it is the one place that refuses hardest.
- **The session id may be filled in once, and never re-pointed.** On a cold spawn the CLI reports its
  id *during* the first send (the `system/init` event), after the pointer is already open, so the
  daemon's session-opened hook stamps it. It writes only into a still-open, still-anonymous pointer:
  re-pointing one would hand a session's turn to a different session, the wrong-attribution failure
  wearing a helpful face.

#### 3.5.3 The three rungs, and the floor

`origin.request`, resolved at `jobs.py start`. Every rung is a **verified** match or an explicit
`unresolved` — never the nearest inbound, never a timestamp-proximity join.

| Rung | Reading | `by` | Carries |
|---|---|---|---|
| **1** | The owner asked, in this message | `owner` | `turn_id` + `message_id` + `surface` |
| **2** | The owner asked, in this turn — no message handle exists (the cockpit; a picker tap) | `owner` | `turn_id` + `surface`, and a `why` naming what is missing |
| **3** | **The assistant started this itself**, in this turn | `assistant` | `turn_id` + `surface` + **`reason_class`** (§3.5.7) |
| **0** | who asked is unresolved | `unresolved` | `why` — the check that failed, in a sentence |

**Rung 3 is a positive claim, not an absence.** The assistant deciding to start a job off the back of
a job-completion notice is a real event with a real location, and it is recorded as one. Consequently
**an `assistant` request never carries a `message_id`**: one of the owner's ids hanging off rung 3
would say the opposite of what rung 3 says. Enforced in `normalize_request`, not left to callers.

**The rung itself is derived, never stored** (`jobs.request_rung`). A stored rung is a second source of
truth that can disagree with the fields it summarises, and the first time it does, the summary is what
someone quotes.

#### 3.5.4 The rule that outranks the feature

**A WRONG ATTRIBUTION IS WORSE THAN NO ATTRIBUTION.** The field is worth having only if *"who asked
for this?"* can be trusted; one plausible-but-wrong link destroys that for every row at once, because
afterwards nobody can tell which of the answers were guesses. So:

**Fail-open on the job, fail-honest on the field.** A stamp that cannot resolve never blocks a spawn —
the read is total (an unreadable pointer, a corrupt one, an exploding `load_json`, all yield `{}`) and
the writers never raise, per this directory's house rule. But it never *approximates*, either. The
seven distinct refusals, each with its own sentence in the record: no pointer at all · the job has no
session id · the turn has not been told its session id yet · the turn belongs to another session · the
turn had already ended · the turn is outside the freshness window · the open time is unreadable. A
reason rather than a bare no, deliberately — an `unresolved` row that says which check failed is
auditable, and a shrug is what makes people start guessing again.

`resolved_by` (`turn-pointer` / `flag` / `none`) is `stamped_by`'s argument one level down, and is
**conservative**: if any field was asserted by a flag the whole row reads `flag`, even where another
field was verified. A provenance marker that over-claims verification is worse than none.

#### 3.5.5 What this deliberately does NOT do

- **No backfill, ever.** Records that predate this have no message identity and no way to recover one.
  An inferred origin would be manufactured evidence in the one log whose entire value is that it isn't.
  They stay absent, they read as rung 0, and a test pins it.
- **No rung 2 for a genuine desktop `/assistant` session.** A desktop session is not served by the
  daemon, so its turns are never teed into `warm-transcript.jsonl` and **it has no trace row and no
  `turn_id`** (the trace's sources are all daemon-written warm-session logs). What a desktop session
  *does* have is its `CLAUDE_CODE_SESSION_ID`, which `origin.session_id` already carries. So a
  desktop-spawned job records `unresolved` with *"not started from inside a warm-session turn"* —
  honest, and the alternative (stamping it with whatever turn the daemon happened to have open) is
  precisely the wrong attribution §3.5.4 forbids. **The cockpit chat pane is a different case and IS
  covered**: it is a third `channel` on the same warm session, so it gets a real rung 2.
  `--request-turn` / `--requested-by` exist as the manual escape hatch, recorded as asserted.
- **No new gate.** Nothing reads `request` to decide anything. It is `status` output and a record
  field, exactly as `origin` was after phase 0.
- **No schema bump.** Still `seneschal.job/1` — an additive key inside a field the schema already
  declares (§3.4).

#### 3.5.6 Does this make the dead-origin-session problem more tractable? Yes — partly

The sibling problem is a job finishing into a session that is no longer live. This does not fix it and
was not meant to, but it changes what is *possible*, and the difference is real:

**A session id is an address that expires. A message id is an address that does not.** A dead
`session_id` has nowhere to route to — the session is gone, its mailbox is a pull nobody will read
(§3.3's stated limitation). A Telegram `message_id`, by contrast, still names a message that is still
in the chat: a completion push for a rung-1 job could **reply to the message that asked for it**, which
puts the answer next to the question on a surface that outlives every session. That is a genuinely new
option, and it did not exist before this phase.

Two honest limits on that optimism. It only helps **rung 1** — rungs 2 and 3 still carry only a
`turn_id`, which is as session-scoped as the session id is. And it is a *delivery* change, which §3.3's
invariant governs: it may only ever **add** a delivery, never make the existing push conditional on
it. Whether to build it is a separate decision with a separate argument.

#### 3.5.7 WHEN rung 3 is legitimate — the four classes

§3.5.3 makes *"the assistant started this itself"* recordable. It does not say **when that is the
right thing to have done**, and that omission is the whole of this section. The question it answers:
in which situations does the assistant start a job without the owner explicitly asking?

A day of real self-started jobs was sorted into four situations and put to the owner. The verdict:
three of the situations were welcome — and not merely tolerated, *welcome*: the owner was glad the
assistant had done that work itself — and one was not.

##### The polarity: classes 1–3 are WANTED, not permitted

The difference is not a nicety of tone — it decides how a future session behaves, and the two readings
produce opposite behaviour:

| If the rule reads… | …the next session does this |
|---|---|
| *"the assistant is ALLOWED to self-spawn for repairs"* | checks first — asks the owner whether it may repair a PR its own merge broke |
| *"the assistant SHOULD self-spawn for repairs"* | repairs it, stamps the class, and tells the owner what it did |

The first is **worse than the behaviour this section documents**. It converts a job the owner was
happy about into a decision they have to make — the judgment is the cost, not the typing — and it is a
friction they have to remember to create.

> **A session that stops to ask permission for class 1, 2 or 3 has misread this section.**
> Do the work, stamp the class, say what you did. A reader should come away MORE willing to
> self-spawn in classes 1–3, not less. Any later edit that sands this into "may" or "is allowed to"
> is reversing the decision, not tightening a rule.

Class 4 is the only prohibition here, and it is a prohibition on **one specific move** — not a general
instruction to be more cautious.

##### The evidence is a hand classification, not a measurement

The taxonomy rests on one turn reconstructing a day's jobs from conversational memory. Nothing counted
them, and **no query can**: the mechanism that would verify them is `origin.request.reason_class`, the
field this phase adds, and §3.5.5 forbids backfilling the rows that predate it. So that cohort is
unverifiable by construction, and it is the last one that will ever be: every job started after this
records its class at spawn, from the turn that made the decision, at the moment it made it.

Two honesty notes the original partition hid:

- **A job whose origin cannot be reconstructed stays unclassified.** An unclassified row is a smaller
  error than a confidently assigned one, and filing it under the nearest-looking class is the same
  manufactured attribution §3.5.4 forbids one field over.
- **A small integer sitting beside a list does not read like a claim**, so hand counts drift from the
  lists they summarise without anyone noticing. Counts in this section are deliberately omitted; the
  recorded classes will produce real ones.

##### The four classes

| # | `reason_class` | What it is |
|---|---|---|
| 1 | `merge-repair` | **Repair of something a merge broke.** A merge the owner approved dirties another PR or reddens CI. The repair is the cost of the tap they already spent — they paid for the merge, not for a second decision about its consequences. |
| 2 | `ruling-durability` | **Converting a chat decision into a tracked file.** The owner decides in conversation; a chat decision binds nothing until it has a tracked file. **The decision is the owner's; only the durability is the assistant's** — which is why this is not a decision being taken on the owner's behalf. |
| 3 | `spec-phase` | **Building the next phase of a spec the owner has already read.** The loosest of the three — it extends furthest on the assistant's own judgement. |
| 4 | `floated-idea` | **A VIOLATION, NOT A CATEGORY.** The owner floated a half-formed idea and it was converted into a job. |
| — | *unclassified* | Origin not reconstructable. Not a `reason_class` value — a row this section declines to assign. |

##### `spec-phase` is the thinnest of the three, and it is thin in a specific way

It carries no bound of any kind: no time window, no size limit, and — the gap most likely to bite — **no
carve-out for a phase the owner has explicitly held.** A spec phase can be held pending a measurement,
so "the next phase of a spec they have read" is not always a phase that is *available* to take. Read the
class as covering a phase that is **unblocked**, not merely written; a phase with a hold on it is a
question for the owner, not a self-start.

##### Class 4: what it actually costs, and why it is a different kind of wrong

The shape: the owner writes a message thinking out loud about a mechanism — several stacked
alternatives, a mid-sentence tangent about what to call it — and within minutes a full specced phase
has been briefed off it as a job.

**Classes 1–3 cost the owner extra pull requests they did not ask for. Class 4 costs them the
conversation.** A half-formed idea is floated *in order to think about it with the assistant*.
Converting it into a job does not merely add work — **it ends the thinking.** The question stops being
open and becomes a brief; the answer the owner would have arrived at is replaced by one the assistant
invented; and the part the owner was actually there for is gone. Building together is not overhead to
minimise. It is the point.

That is why this is not "class 3 but a bit further" and cannot be graded on the same axis. Classes 1–3
spend the wrong resource (the owner's review queue). Class 4 spends the *right* resource in a way that
destroys it.

##### A job can carry more than one class

The table reads like a partition. It is not one. A single job can be started because the owner
explicitly asked for one thing (*"if that requires some spec rework, start with that"* — unambiguous,
rung 1) and then have a floated idea from the same conversation folded into the same brief. That one
job is, simultaneously:

- **asked for** — the half the owner requested in words; and
- **`floated-idea`** — the half the owner was still thinking out loud about.

Neither reading deletes the other. The ask does not launder the fold-in, and the fold-in does not
retract the ask. **The class that belongs on that record is `floated-idea`**, because the violation is
the part worth being able to see and a record showing only the half that was asked for would show
nothing — but the record is *lossy* about it, and a reader should learn that here rather than infer a
clean partition from a table.

The operational consequence: **when a job is part ask and part self-start, stamp the self-start.**
`reason_class` is single-valued and stays that way; the field exists to surface what the owner cannot
otherwise see, and what they can already see is the thing they typed.

##### The tell is GRAMMATICAL, not semantic

This is what makes the rule checkable rather than a matter of taste. Look at the **syntax**, not at how
good the idea sounds or how confident it makes you feel about building it:

- **stacked alternatives** — *"it could be X or it could be Y"*
- **hedges** — *"maybe"*, *"I think"*, *"or whatever"*
- **a question the owner asks themselves mid-sentence**
- **a terminology tangent** — *"poller? pollster?"*

None of them is a request.

> **An idea in exploratory syntax is not a request. The correct response is a question back, not a
> job id** — specifically: *which of your alternatives did you mean, and what draws you to it?* The
> owner's answer will be better than the invented one, and that is not politeness, it is accuracy: the
> invented answer is a guess at a question they had not finished asking.

**No detector, no classifier, no heuristic.** Nothing in `jobs.py` reads the owner's text and decides
whether they were brainstorming. That is a judgement made at compose time by the turn that is holding
the conversation, with everything the turn knows — and a regex over hedge words would fail in both
directions, licensing the spawn that was not wanted (hedged, and still meant it) and blocking the one
that was. The deliverable of this section is a **rule and a recorded class**, not an automaton.

##### The design: a required reason class on rung 3

`origin.request.reason_class`, closed vocabulary, present on **every** rung-3 record:

```
reason_class  =  merge-repair | ruling-durability | spec-phase   # the approved three
              |  floated-idea                                    # a VIOLATION, recorded as one
              |  unstated                                        # the honest floor
```

**Should class 4 be representable at all?** Yes, and this was the one question worth getting right. If
the only legal values were the three approved classes, a class-4 spawn could not be recorded as itself
— it would be filed as whichever approved class sat nearest (an auto-rebaser is *about* merges, so it
would land as `merge-repair` and look unremarkable). **The one thing the owner asked to be able to see
would become the one thing invisible. An unrepresentable failure is an undetectable one.** So the
vocabulary carries it, marked as a violation rather than as a category, and every surface that prints
it says so: `list` and `status` both print a loud self-started-off-an-idea marker — an answer you only
get by already knowing which job to ask about is not an answer to *"which situations does this
happen in?"*.

The value's real function is not the record, though. It is a **speed bump at compose time**: the only
legal way to start that job is to type the confession. A turn that finds itself reaching for
`--reason-class floated-idea` has been handed the finding it needed — the spawn is the wrong move, and
the question back is the right one.

Five design consequences, each of which an ordinary later edit would get wrong:

- **`unstated` is a value, not an absence.** A rung-3 row whose turn named no class is a real state,
  and it must not be indistinguishable from a row written before this existed. `normalize_request`
  supplies it at the single write point, and an *unrecognised* value becomes `unstated` too — never
  coerced toward an approved class, which is the same refusal `by` makes one field up.
- **Required in the record, not enforced by refusing to spawn.** `--reason-class` is optional at the
  CLI and a missing one costs the class, not the job. §3.5.4 governs: fail-open on the job,
  fail-honest on the field. A stamp that could block a spawn is a stamp that will one day block a
  repair.
- **`reason_class` and `why` answer different questions and neither may be written into the other.**
  `why` is why a *resolution* failed; `reason_class` is why *the assistant started it itself*. A rung-3
  row can carry a class and no `why` at all.
- **It is outside `resolved_by`'s scope**, deliberately, and this is the one place the conservative
  rule of §3.5.4 does *not* extend. `resolved_by` says how the **identity** fields were learned, and
  identity is the thing that can be verified. A reason class never can be — it is the turn's judgement
  about its own behaviour — so naming one must not downgrade a pointer-verified row to `flag`. A
  provenance marker that punishes candour will stop being fed.
- **No retro-labelling, for the reason §3.5.5 gives.** Going back to mark an old record would be the
  same manufactured evidence as a backfill, in the same log.

##### THE OWNER'S TURN IS NOT THE OWNER'S CONSENT — the gap this closes

The requester ladder attributes by **turn provenance**: the pointer's `by` records who composed the
line that opened the turn (§3.5.2). That is right for what it measures and *wrong* as a proxy for
consent, and class 4 is precisely where the two come apart.

A class-4 spawn happens **inside a turn the owner opened** — they were thinking out loud on Telegram,
so the pointer says `by: "owner"` with their `message_id` on it. Left unasserted, the job records
**rung 1 — "the owner asked, in this message"**: the strongest attribution the ladder has, for a job
they never asked for. That is a wrong attribution of exactly the kind §3.5.4 forbids, manufactured not
by a bad join but by an assumption nobody stated — *their turn ⇒ their request*. A test pins the trap
and its fix.

The same applies to classes 1–3 whenever they happen mid-conversation, which is most of the time. So
the operational rule is:

> **Pass `--requested-by assistant --reason-class <class>` whenever YOU decided to start the job,
> regardless of who opened the turn.** The pointer records who wrote the line. It cannot know whether
> the line was an instruction, and only the turn reading it can.

The record then reads rung 3 with the turn still verified from the pointer and **no `message_id`** —
`normalize_request` strips it, because one of the owner's ids hanging off a rung-3 row would say the
opposite of what rung 3 says.

##### LIMITATION: the owner ratified the JOBS, not the PARTITION

This is the load-bearing caveat on the whole section.

**What was decided** is that *those specific jobs were welcome, and that specific one was not*, plus
the polarity. That is a verdict on the concrete jobs shown, and it is solid.

**What was not decided** is that these four names carve the space correctly. The four classes were
**the assistant's classification, put to the owner as a yes/no about the jobs inside them.** Agreeing
that a set of jobs was fine is not the same act as endorsing the categories used to sort them, and the
difference is invisible in a "yes". Two gaps follow immediately:

- **A job can fit two classes at once** — above. A partition whose own canonical instance is not
  partitioned is not yet a partition.
- **A fifth situation may exist and simply did not arise in the sample.** Worse, **the sample does not
  fully constrain even itself**: a job whose origin cannot be reconstructed is not known to be a
  self-start at all, let alone which class it would have taken.

**What would settle it, and why this is a limitation rather than a defect:** the recorded classes
accumulate. Every rung-3 job stamps its class at spawn, `unstated` included, and after enough of them
the partition becomes checkable **against real data instead of memory** — which rows had to be forced
into a class that did not fit, how often a turn that knew exactly what it was doing still chose
`unstated`, and whether a shape shows up that none of the four names reach.

**Until then this taxonomy is a well-evidenced proposal, not a settled ontology.** An edit that adds,
splits or renames a class is doing ordinary work. The two things that *are* fixed, and which such an
edit may not touch, are the **polarity** — classes 1–3 are wanted, not merely permitted — and the
**class-4 prohibition**.

Deliberately left open; see §9.4.

##### Where this lives, and why in two places

The taxonomy, the evidence and the reasoning are here. **The trigger is in `../modes/chat.md` rule
10** — the class names, the grammatical tell, and the one sentence about what class 4 costs. That
duplication is deliberate: a rule that lives only in a spec is not reachable at the moment of the
decision. `modes/` is what a live turn reads imperatively on dispatch; a spec is consulted. The
reasoning belongs where it can be argued with; the rule belongs where it will be read while a job is
about to be started.

---

## 4. The payload contract — the load-bearing part

The analysis is valuable **because the analyst is ignorant of the working agent's reasoning.** That is
not a limitation to be engineered away; it is the entire product. This section exists to design
against the obvious, well-meant future "improvement" — *"the analyst would do better if we gave it the
context we already have"* — which converts a fresh set of eyes into a slower copy of the agent that is
already stuck, and does so while looking like a kindness.

### 4.1 What is sent TO the analyst

**Exactly two things: the artifact, and ONE LINE stating the goal.**

- **The artifact** is a **path**, not pasted contents: the job's `.log`, its `.json` record, a stack
  trace, a run manifest. A path is enough because the analyst has `Read`/`Grep`/`Glob`, and a path is
  *better* because it lets the analyst find the nearest succeeding counterpart itself — often the most
  productive method.
- **The goal line** is `origin.goal`: one line, what the work was trying to achieve. Without it the
  analyst is guessing what "working" would have looked like.

**Explicitly NOT sent:** the originating session's transcript, its hypothesis, its prior attempts, its
diagnosis-so-far, the job's own `attempts[]` narrative, or any "here's what we think happened."

That last one deserves its own sentence, because it looks harmless. `attempts[]` is mechanically
recorded fact — but it contains `classification: "transient"`, which is `jobs.py`'s *verdict*. Handing
that over pre-frames the search ("this was an API blip") exactly as a human hypothesis would.

**The cut that makes this coherent: ignorance is about what we PUSH, not about what exists.** The
analyst may read anything on disk, including the full record and every sibling job. Nothing is hidden.
What it is not given is a *starting point that isn't its own*. Pull is fine; push is contamination.

**Enforced by a signature, not by a request.** The delegation string is built by one pure function:

```python
def build_delegation(artifact_path: str, goal: str) -> str: ...
```

Two parameters, both scalars, one fixed template. There is no argument through which a transcript can
arrive, so the contract cannot be violated by forgetting it — only by editing the signature, which is a
reviewable act. This is the `metrics.jsonl` lesson applied at the right layer: **a contract that lives
only in a prompt gets zero rows in a month; a contract that lives in a function signature is kept by
construction.** A unit test asserts the built string contains the artifact path, the goal line, and
nothing else.

`origin.goal` is newline-stripped and length-capped (≈200 chars) **at write time**, in `start_job` — so
"one line" is structural, not aspirational. A caller who pastes four paragraphs of hypothesis into
`--goal` gets one line stored. Enforce where it's written once, not where it's read N times.

### 4.2 What comes BACK

A ranked list of **locations to examine**, and no claim about what is wrong.

```json
{
  "schema": "seneschal.job-leads/1",
  "job_id": "<job id>",
  "analyst": "warm",
  "produced_at": "<ISO-8601 timestamp>",
  "contamination_present": false,
  "leads": [
    {
      "rank": 1,
      "location": "seneschal/scripts/rag_projects.py:212",
      "evidence": "The succeeding run logged 41 'indexed' lines here; this run logged none, and the loop's only exit before that point is the roots read at :188.",
      "basis": "established",
      "cited": ["state/jobs/<this job>.log", "state/jobs/<the succeeding job>.log"]
    }
  ],
  "notes": ""
}
```

**The schema makes "point, don't diagnose" structurally impossible, not merely requested:**

- **There is no `cause` field. No `fix`, no `patch`, no `root_cause`, no `confidence`.** The absence is
  the mechanism. An analyst inclined to diagnose has nowhere to put it.
- **Unknown keys are rejected, not ignored.** A returned `"cause"` doesn't get silently dropped — it
  makes the artifact **invalid**, which makes the delegation a **failed** one. Tolerating the extra key
  would teach the analyst that the rail is decorative.
- **`basis` is a required enum**, and the validator *enforces the ordering* — every `established` item
  sorts before every `conjecture` item — rather than trusting the model to have ordered them. An
  `established` lead must carry a non-empty `cited`: "I verified this" is only meaningful with what was
  read attached.
- **`contamination_present`** is required and defaults to `false`. If the delegation carried someone
  else's reasoning, the analyst says so at the top and ignores it. Making the field mandatory means the
  routing can *count* contamination events instead of hoping they don't happen.

The validator ships **before** the producer (§7, phase 1), deliberately: the schema is the product, and
a schema written after the first artifact will be shaped to accommodate whatever the model happened to
emit.

### 4.3 One pass

**The analyst answers once.** No reply-to-the-reply — two agents that can address each other is a very
expensive chat room. Built out of three cuts, none of which is a rule anyone has to remember:

1. **The analysis artifact has no `origin` field.** It cannot address anything back.
2. **An analysis job is spawned with no `origin`** and `jobs.py` refuses one. So an analysis of an
   analysis cannot be routed, and the loop cannot close. The precedent is `session_stamp.py`'s
   recursion-env guard — *never dream the dreamer* — one line, unbypassable.
3. **There is no reply channel to build.** A chat room would have no terminal state, and "every job
   reaches a terminal state" is the guarantee the whole module exists to make.

### 4.4 An empty list is a valid answer

`"leads": []` is **valid, cheap, and correct** when nothing stands out. The validator must not treat it
as failure; the executor must not retry on it; the completion push must report it plainly ("nothing
stood out") rather than as an error.

This is the mirror of a lesson in `../references/archons.md`: **a zero exit from a delegation is not
success**, because an archon can return prose and stage nothing. Both halves are the same rule —
**judge the artifact, not the exit code.** A well-formed empty artifact is success; a zero exit with no
artifact is failure. Manufacturing leads to avoid an empty list is strictly worse than silence, because
someone spends real time testing a fabricated one.

---

## 5. The A2A shape

The framing maps cleanly onto agent-to-agent vocabulary:

| A2A | here |
|---|---|
| **Task** | the job (`state/jobs/<id>.json`) |
| **Artifact** | the analysis (`state/jobs/<id>.analysis.json`, `seneschal.job-leads/1`) |
| **addressee** | `origin` |

The sibling `demiurge` repo (`../references/archons.md`) speaks A2A natively — delegation rides A2A and
tools ride MCP. So when an archon is the analyst, **the delegation is already an A2A call**; that part
is reuse, not new work.

**What would be a local convention: the return address itself.** A2A addresses a client that is
*waiting on the task*. Here the client is usually dead before the artifact exists (§3.3). A2A's
push-notification config could carry a webhook, but that needs a listener per session, and standing up
an HTTP server per Claude Code session to receive one JSON file is not a trade worth making.

**Do not over-engineer: for v1, a JSON artifact on disk plus the existing queues is enough** — and it
is what the daemon already does for everything else it owns. An A2A SDK is a dependency of demiurge,
not of this framework, which is stdlib-first with a short list of sanctioned dependencies. Speaking A2A
natively here would add one, for a feature whose v1 needs a file write.

**What A2A would buy later:** if a surface ever becomes genuinely addressable, the artifact shape maps
1:1 onto an A2A Artifact and delivery becomes a **transport swap, not a redesign**. The one realistic
candidate is the **cockpit**, which already holds a live websocket to a live client — a lead list
rendering in a panel next to the Jobs panel is the version of this feature a human would actually
watch. Design the artifact so that swap stays cheap; don't build the transport.

---

## 6. Who does the analysis

The intended posture for the analyst is a charter of six hard rails: never diagnose · contamination
refusal · evidence-not-opinion and read-only · established-vs-conjecture · empty-is-valid · one pass —
with read-only tool grants (`Read`/`Grep`/`Glob`/`Bash(git log *)` and nothing else).

**v1 depends on no archon.** Two paths land independently:

- **Routing without an analyst archon** — the analyst is the warm session's one-shot delegation path
  (`--analyst warm`, the only value in `job_analysis.ANALYSTS`). The gain is the *filing*, the
  *schema*, and the *address*; those are most of the value and none of them need an archon.
- **An analyst archon without routing** — delegating the artifact path plus the goal line to a
  dedicated archon by hand works the day one is admitted.

**The seam:** the `--analyst` switch on whatever executes the analysis, defaulting to `warm` and
**refusing any other analyst in code unless that archon's record says admitted**. A charter that has
not passed its evals is not a posture, it's a hope.

One note the archon boundary earns rather than assumes: **the ignorance is structural only across that
boundary.** A subagent spawned inside the working session inherits that session's context and therefore
its sunk cost. A delegation-on-demand archon starts clean by construction. So "just use a subagent" is
not a cheaper version of this — it is a different thing that doesn't work.

---

## 7. Phasing — smallest useful first, merge on green

Every phase is independently landable and independently useful. Phase 0 is worth merging even if
nothing after it is ever built.

**Phase 0 — stamp it. No behaviour change.**
`--origin-session` + the `CLAUDE_CODE_SESSION_ID` auto-stamp + `--goal`, written into `origin` with the
§3.2 shape and the one-line cap. Registry enrichment (`source`/`cwd`/`branch`) copied in when an entry
exists. `jobs.py status` prints it. **Nothing reads it.**
*Why first:* the parent spec's §7.4.1 argument, applied one feature later — start collecting the data
**before** the decision it informs, not after. Weeks of populated `origin` also answer "does the
auto-stamp actually fire?" (that's what `stamped_by` is for) before anything depends on the answer.

**Phase 1 — the artifact and its validator. Pure functions.**
`seneschal/scripts/job_analysis.py`: the `seneschal.job-leads/1` schema, the strict validator
(unknown-key **rejection**, `established`-before-`conjecture` ordering, `cited` required on
established, empty-list valid), and `build_delegation(artifact_path, goal)`. No delivery, no analyst, no
I/O beyond reading a candidate artifact. Fully unit-testable, no spend, no network.
*Why before the producer:* §4.2 — the schema is the product.

**Phase 2 — the analyst, warm-session flavour.**
`jobs.py analyze <id>` (and/or `--analyze` on `start`, for a job that wants it on failure): on a
terminal `failed` / `ended-unknown` job (§11), spawn a **second, origin-less job** whose command
produces the artifact through the warm session's existing delegation path, then validate and stage it
as `state/jobs/<id>.analysis.json`. Recursion refused in code (§4.3). No new delivery yet — the
artifact lands on disk and the existing push names its path.

**Phase 3 — delivery. The §3.3 ladder.**
Rung 2 (`wake_text` gains the artifact path), rung 3 (`state/session-mail/<session_id>.jsonl` + the
orientation read in the `/assistant` command and the daemon's grounding block), rung 4 (verify by test
that it is untouched). Plus the Dream sweep (`jobs.py prune --mail-days`).
*The regression test that matters here:* the completion push still fires exactly once on every
terminal state, with routing on, with routing off, and with a route that fails.

**Phase 5 — WHO ASKED. `origin.request`.**
The §3.5 requester ladder: `state/current-turn.json` written by the daemon where `turn_id` is minted and
closed in the same `finally` as the interleave window; `jobs.build_request` resolving it against
`$CLAUDE_CODE_SESSION_ID`; the three rungs plus the `unresolved` floor with a reason on every path;
`--request-turn` / `--requested-by` as the asserted escape hatch; `jobs.py status` printing it in
words. **No backfill** (§3.5.5).
*Why it is a phase here rather than its own spec:* it writes into `origin`, whose shape §3.2 enumerates
and whose round-trip rules §3.4 sets. Splitting one field's definition across two documents is how the
two get edited apart.
*Why it is numbered after phase 4 despite landing first:* phase 4 is gated on an external event (an
analyst archon's admission), not on anything here, and renumbering would break every reference above.

**Phase 4 — an analyst archon. Unbuilt.**
`--analyst <archon>`, admitted-gated (§6), executed in the proven detached-archon shape: deploy
detached → poll the agent card → delegate → **validate the staged artifact** → tear down. Score every
delegation with the archon verdict loop so curation actually runs: a lead that pointed at the right
file is a hit; a confident diagnosis is a violation *even when it happens to be right*; a manufactured
lead that wasted a test is a violation with a measurable cost.

---

## 8. What is NOT in scope for v1

- **No reply path.** §4.3. One pass, by construction.
- **No cockpit surface.** The artifact would render well next to the Jobs panel; that is a
  cockpit-side follow-up on the cockpit's side of the boundary, and nothing here blocks it.
- **No automatic analysis of every failure.** Opt-in per job. An analysis is a model call, the budget
  governor meters spend, and "analyse every red job" is a quota decision, not a routing one — it
  deserves its own decision with its own numbers.
- **No push to a `build` or `desktop` session.** There is no channel (§1.1) and inventing one is a much
  larger change than this feature justifies. Pull mailbox or nothing.
- **No cross-machine addressing.** One box, one state dir.
- **No A2A transport.** §5.
- **No `origin` on the analysis job itself.** That's the recursion guard, not an oversight.
- **No change to the completion push's guarantee, ever.** §3.3.

---

## 9. Open questions

1. **Opt-in or automatic?** *Decided: opt-in.* The counter-argument is that opt-in is how `origin`
   stayed empty — but the counter-counter is that this one costs money. Worth revisiting once phase 0's
   data says how many jobs actually fail.
2. **Mailbox retention.** *Decided: 14 days*, matching `jobs.py prune --days`, one named constant.
3. **Is `--goal` mandatory when analysis is requested?** *Decided: yes.* An analysis with no goal line
   is an analyst guessing what "working" would have looked like, and §4.1 says the goal line is half the
   input contract.
4. **Do §3.5.7's four classes actually carve the space?** The **polarity** (classes 1–3 are wanted) and
   the **class-4 prohibition** are fixed, but **the owner ratified the jobs, not the partition** — the
   categories were the assistant's, put to the owner as a yes/no about the jobs inside them. A canonical
   instance already carries two classes at once. **Not a question to answer now:** the accumulating
   `reason_class` records answer it with data instead of memory. §3.5.7's limitation note.

---

## 10. Where reading the code contradicted the first design

1. **"Auto-stamping from the session registry"** — the registry *cannot* identify the caller. Every
   hook-stamped entry carries `pid: 0` by design (`session_stamp.py`), and `cwd`+`branch` is routinely
   ambiguous. The identity lives in the **environment** (`CLAUDE_CODE_SESSION_ID`, byte-identical to a
   registry filename), so the registry's role is enrichment (`source`/`cwd`/`branch`), never identity.
   §3.1.
2. **The return path cannot reach a non-daemon session.** The daemon's enqueue path feeds only its warm
   session; a desktop `/assistant` and a `build` session have no queue, no drainer and no socket. That
   reframes the whole feature from *delivery* to *filing plus pull*, and it is the single biggest
   correction here. §1.1, §3.3.
3. **"The existing Telegram push is the floor"** — correct, and stronger than it sounds: the push
   already fires unconditionally off the record, so the floor needs **no new code** and can only be lost
   by someone deliberately making delivery conditional. Worth pinning with a test precisely because it is
   free.
4. **"`origin` … no CLI path populates it"** — true, and more completely than that: there was **no
   reader anywhere** either, and every record carried `"origin": {}`. The field was inert in both
   directions, which is why phase 0 is safe.

---

## 11. Where reading the code contradicted THIS spec (found while building phases 0–3)

Same discipline as §10, one layer down. Four things, all small, all resolved toward preserving the
pre-existing behaviour — plus a fifth (§11.1) that the spec never said at all, which was the problem.

1. **`origin.source` reads `build` for a job started from a warm turn, not `daemon`.** §3.3 rung 2 keys
   on `source == "daemon"`, but the daemon's warm session is itself a Claude Code session, and
   `session_stamp.py` writes **every** session as `source: "build"` with no exemption. The registry
   entry keyed `daemon` is written by the daemon itself under the id `daemon`, which is *not* the
   `CLAUDE_CODE_SESSION_ID` a warm turn's child inherits — so enrichment finds the hook's `build` entry,
   and rung 2 as specified would essentially never fire. **Resolution:** `route_analysis` treats
   `source == "daemon"` **or a `--wake` job** as daemon-shaped. `--wake` means "read this back to me in
   voice," which is exactly an address at the warm session, and it cannot be wrong: the rung it selects
   only ever adds a delivery. (`cancel-attribution-spec.md` §4.2's `SENESCHAL_SESSION_SOURCE` fixes the
   root cause for `source` itself.)
2. **`stamped_by: "none"` was unreachable as specified.** §3.2 lists it as a value, but §3.1's ladder
   ends in "absent → `{}`": no id and no goal must produce `origin: {}` exactly. `"none"` is now
   reachable in the one case that is genuinely neither — a `--goal` with no session id, i.e. context
   without an address.
3. **`job.example.json` shipped an `origin` no code has ever produced** — `{"source": "warm"}`. `warm`
   is not in `sentinel.GATING_SOURCES` and is not a value anything writes. Corrected to the §3.2 shape.
4. **The analysis is filed when the ANALYSIS job ends, not when the analysed job does.** §3.3 says the
   ladder is "evaluated when the job reaches a terminal state", which reads as the analysed job; but
   phase 2 makes the analysis a *second* job, so the artifact does not exist yet at that moment. Routing
   therefore fires on the analysis job's terminal state, reading the address off its target. The
   invariant is unchanged — both jobs push exactly once, and routing is strictly downstream of both.

**One deliberate addition beyond the letter of §4.2:** the validator also requires lead `rank`s to be
**distinct**. Two rank-1 leads is not a ranking, and it is the kind of thing a model emits when it stops
paying attention to the ordering rail the schema does enforce.

**One thing §7 phase 2 named that is deliberately narrower here:** `TIMED_OUT` is not in
`jobs.ANALYSABLE`. A job that was merely too slow leaves no readable failure for an analyst to point at.
It is one line to widen.

**And `cancelled` is not in the list either** (`background-jobs-spec.md` §3.10), so `ANALYSABLE` is
`failed` / `ended-unknown`. A cancel is not a failure to investigate: the analyst is handed a log plus
a goal line describing work that was *deliberately abandoned* and has no way to tell — the payload
contract that keeps it ignorant of the working agent's hypothesis also keeps it ignorant of this. So it
would spend a real one-shot on an incident that never happened and file ranked leads about it. The
session that cancelled the job already knows why. The manual `jobs.py analyze <id>` is governed by the
same constant and refused with the same sentence, deliberately.

### 11.1 Where the analysis job RUNS — unspecified, inherited, and wrong

Nothing above ever said what working directory the analysis job should get, so phase 2 inherited the
analysed job's: `request_analysis` passed `cwd=rec.get("cwd")`. That was harmless until
`delegated-work-isolation-spec.md` phase 1 shipped `--worktree`, after which a `--worktree` job's `cwd`
**is the private worktree** — and `jobs.release_worktree` tears it down at the terminal transition,
before the completion push, which is before `request_analysis` runs. The symptom: an analysis job
recorded with the torn-down worktree as its `cwd`, `"status": "failed"`, and a spawn error saying the
directory name is invalid.

**This was not a cancellation quirk.** Teardown runs on every terminal state, so **`--analyze` combined
with `--worktree` failed to spawn essentially always** — the exception being a worktree whose removal
had been *refused* (a dirty tree ⇒ `worktree_leaked`), i.e. it only worked when something else had
already gone wrong. That inversion is the tell. And the failure was quiet: the completion push had
already fired correctly, so from the outside nothing was broken, while the `--analyze` safety net
silently did not exist for exactly the jobs most likely to need it — long, delegated, isolated build
work.

**The fix: choose, never inherit.** `jobs.analysis_cwd(rec)` picks `worktree.host` (the checkout that
owns the tree, recorded by `job_worktree.create` and untouched by teardown), then `REPO_ROOT`, then the
temp dir as a guaranteed-to-exist floor — each candidate verified with `os.path.isdir` immediately
before the spawn, because the whole bug was a path that existed when it was recorded and not when it
was used. A missing directory must never be the thing that costs the analysis.

**The analyst loses nothing by this**, which is why it is a cwd-selection fix and not a redesign: its
entire input is an artifact **path** plus the goal line (§4.1), `--state-dir` is already absolute, and
`job_analysis.run_analysis` *already* spawns its `claude` one-shot with `cwd=jobs.REPO_ROOT`. The
analysis job's own working directory was incidental all along; depending on a disposable one was the
defect. Every guarantee above is unchanged — the analysis job is still origin-less (§4.3), the
completion push is still unconditional and exactly-once (§3.3), and a failed analysis still costs only
the analysis.

The general rule this leaves behind, for anything that spawns off a finished job's record: **a terminal
job's `cwd` is not a durable path.** It may be a directory the cleanup pass has already reclaimed.

## Router entry

**Status:** PARTIAL(phases 0-3,5 BUILT).

**What it decides:** A job's return address; the fresh-eyes analyst that points without diagnosing.
Phase 4 waits on an admitted analyst archon. **Phase 5 is the OTHER address — not which session, but
WHO ASKED** (§3.5). `origin.session_id` could never answer it: one warm session serves hundreds of
turns. Three rungs — the owner's message (`turn_id` + Telegram `message_id`), the owner's turn with no
message handle (the cockpit; a picker tap, whose `message_id` names *the assistant's* question), and
**the assistant started it itself**, which is a positive claim carrying a turn, not an absence. **The
mechanism is a turn pointer, because the environment structurally cannot say**: a warm session is one
long-lived process, so `CLAUDE_CODE_SESSION_ID` is fixed for its whole life and there is no per-turn
variable to inherit. **The rule that outranks the feature is `A WRONG ATTRIBUTION IS WORSE THAN NO
ATTRIBUTION`** — seven named refusals, each with its sentence in the record, and a message id resolved
only by a *proved-unique* reverse lookup. **No backfill, ever.** §3.5.6: a session id expires and a
`message_id` does not, so a rung-1 completion push could reply to the message that asked. **§3.5.7 says
WHEN rung 3 is LEGITIMATE, and its polarity is the thing a later edit will get wrong**:
`merge-repair`, `ruling-durability` and `spec-phase` are **WANTED, not permitted** — a session that
stops to ask permission for one of them has misread the section. The fourth, `floated-idea`, is a
**violation and is representable ON PURPOSE**, so the one thing the owner asked to see is never filed
as the nearest approved class. **It costs the conversation, not a pull request** — and **the tell is
GRAMMATICAL** (stacked alternatives, hedges, a terminology tangent), so the answer is a question back,
not a job id. **No detector, no classifier** — the judgement is the turn's. Two more traps: **the
owner's turn is not the owner's consent** (the pointer records who wrote the line, so an unasserted
class-4 spawn records as *rung 1*), and `unstated` is a **value, not an absence**. The trigger a live
turn actually reads is `../modes/chat.md` rule 10.
