# Ask provenance — the outbound question with no recorded author

**Status:** `PARTIAL(phases 1-3 BUILT; 4 open)` — phase 4 is a policy question left to the owner.

A companion to the job-origin work, which gave a *job* a return address; this asks the same question
of an *ask* — a Telegram picker (question + tap-to-answer options) sent in the assistant's voice.

**Subject.** A picker can go out in the assistant's voice, be factually wrong, offer actions the
assistant has no channel to perform — and its record can name no author. This spec establishes why
that is possible, how little provenance the ask store retains by default, and the fix.

**What shipped.**

- **Phase 1** — the ask module (`telegram_ask`) writes an `origin` stamp unconditionally onto every
  question record and onto its tombstone (`build_origin` / `stamp_origin` / `record_origin`), with
  `--origin-session` as the explicit override; the daemon's Watch-peek launch marker names the child
  it spawned.
- **Phase 2** — after a landed send, `ask()` appends one `kind: "question"` row to the assertion log
  (`state/assertions.jsonl`, the record of what the assistant has actually said), carrying the rendered
  **body**, the question id, and phase 1's `origin`. The write is wrapped in its own try **at this one
  site**: an exception escaping here would surface to the caller as *"the ask failed"*, and a caller
  that retries failed asks would ask again.
- **Phase 3** — the assertion first, then the check. The assertion is
  [`../references/comms-mapping.md`](../references/comms-mapping.md) § *What the assistant cannot
  reach*, stated as facts about what exists. The check (`ask_no_access`) holds the same surfaces as a
  tracked constant, matched **literally, case-folded, at word boundaries, against option text only**,
  writing a `no_access` key on the record and one stderr line per finding, **refusing nothing**. A
  test binds the two halves so neither drifts into a claim the other does not make.

  Measured limit, stated rather than papered over: in the motivating case only **half** of the
  options matched — the rest promised to *"tell the other side"* or *"send your apologies"*, named no
  surface at all, and were exactly as unkeepable. That gap is §4.3's untractable half and is not
  being closed.

**Phase 4 is open, deliberately** — see §6 and §9. Nothing here refuses an ask.

---

## 1. The failure shape

The motivating case: a Watch-mode comms peek — a cheap one-shot model session the daemon spawns on
cadence — read the tail of the Telegram thread, concluded two of the owner's meetings were
double-booked, and sent a four-option picker offering to move, shift, or cancel one of them, most
options promising the assistant would notify the other party.

**Three things were wrong, and they are three different failures:**

1. **The fact was wrong.** The two meetings were sequential, not concurrent. The peek had read a
   byte-truncated tail of the thread (`tail -c`, starting mid-word) and inferred the conflict.
2. **Every option promised an action the assistant cannot take.** The counterparties were an
   employer and that employer's client — organisations the assistant has no channel to at all. §5.
3. **The record carried no author.** No `meta`, no source, no session, no cwd. §3.

The rest of this spec is about (3), because (3) is what makes (1) and (2) unanswerable after the fact.

### 1.1 The calendar read that "supported" a false belief

The peek did check the calendar. The read **succeeded and returned zero events**, and the peek read
that zero as *"not on the calendar yet"* — a reading under which an empty result is **consistent
with the conflict being real**, so the belief survived contact with the only evidence available to
test it.

The tree already carries a rule against the opposite error — *a read that succeeds with zero events is
an empty day, not a missing integration* ([`../references/calendar-mapping.md`](../references/calendar-mapping.md)
→ *Empty vs missing*). This was a third reading: a calendar the bridge does not read is absent from a
successful read, and that absence is not evidence about it. That reading is now written into the
channel map; the fix this spec proposes is not aimed at it.

---

## 2. Q1 — who sent it

**Attributable — but not from anything the assistant keeps.**

The sender was identified only from **Claude Code's own session transcript** under
`~/.claude/projects/…`: a file outside this repository, written by the harness, read by no framework
code, with its own retention that nothing here controls.

Inside the framework's own records, the sending session's id appeared in exactly one place: the
instructions-loaded hook log — the hook that records which `CLAUDE.md` files entered context and
**decides nothing**. The question store, the assertion log, the turn log, the session registry, the
job records, and the daemon's own log were all silent on the author.

**So the honest answer is two-part, and the second part is the serious one:**

- *Who sent it:* the Watch peek.
- *Could the framework's records have said so:* **no.** An outbound message in the assistant's voice,
  offering to contact third parties on the owner's behalf, was traceable only because a harness
  artifact happened to still be on disk.

### 2.1 Three near-misses

**The daemon that spawned the sender did not record what it spawned.** The peek launch logged a bare
`• comms peek launched` marker — no child identity, no exit, no outcome.

**The session registry is not a reliable fallback.** Two byte-identical peeks a few minutes apart were
observed, one with a `state/sessions/` entry and one without. A reader consulting the registry would
have answered for one and not the other. *(Why is not established here — §8.)*

**The environment already knew.** The daemon's child environment for a Watch spawn sets a
session-source variable (`SENESCHAL_SESSION_SOURCE=watch` in this design) as *a safety interlock
rather than a label*. The sending process carried its own surface identity in its environment while
writing a record that did not mention it. §4 turns on this.

---

## 3. Q2 — can an ask go out with no provenance, by design?

**Yes, by construction — and more interestingly, provenance was recorded on *none* of them, including
the ones that looked attributed.**

### 3.1 The census

In one install's question store (a rolling 7-day window — `QUESTION_TTL_DAYS`, swept lazily), roughly
**96% of records carried `meta`**, and **0% named a source, an author, a session, or a cwd.**

Every `meta` key present described the **subject** of the question — `pr`, `repo`, `head_sha`,
`approve_index`, `kind` on merge pickers; a date and a kind on a digest-approval picker. **None
described its author.** A reader can *infer* the merge-approval code path from
`kind: "merge-approval"`, because only one component emits that string — but that is inference from a
schema, not a recorded fact, and it says which *code path* built the question, never which *session*
invoked it, from which checkout, on which branch.

### 3.2 What the record retains

Before phase 1, every record carried the same top-level fields, plus optional `meta`:

```
question · body · options · multi · recommended · chat_id
message_id · asked_at · selected · answered_at          [+ meta]
```

No caller field of any kind — no session id, cwd, pid, argv, surface, env-file, or `stamped_by`. The
**tombstone was thinner still** — `{id, question, expired_at}` — so a week after an ask even its
options are gone, and its author was never there to lose.

### 3.3 Why `meta` is the wrong place

`--meta` is *opaque caller data*, optional in both directions: parsed only if given, written only if
not `None`, and **never interpreted** by the picker module. That is correct for `meta` — it keeps a
picker a picker — and it is exactly why `meta` cannot carry provenance: **an opaque field the caller
may omit cannot guarantee a non-omittable fact.**

The callers reflect that. Code paths (the merge guard, a digest-approval skill) pass `--meta`; **every
prose-side caller documents the invocation without it** — the picker sections of
[`../modes/chat.md`](../modes/chat.md) and [`../references/comms-mapping.md`](../references/comms-mapping.md)
spell out the full command line and neither mentions `--meta`. The door most likely to be used in the
assistant's own voice, mid-conversation, is documented in a shape that records nothing.

### 3.4 The second missing record: no picker reached the assertion log

The assertion log (written by the `mouth` module) is the append-only record of what the assistant has
*actually said* to the owner, written in code at each send site after a landed send. In the measured
install it held thousands of rows across six kinds — `reply`, `reminder`, `job`, `archon`, `alert`,
`nudge` — and **no question kind**. Neither the picker module nor the resident PR sweep that sends
merge pickers unasked called it.

So the question store was not merely missing an author field: it was the **only** place a picker was
written down at all, and it is a work queue for resolving taps, not a record of what was said.

---

## 4. Q3 — what would fix it

### 4.1 (a) Require `--meta` with a mandatory source field — **rejected as the mechanism**

This repo's job-origin work already names why: **a field a caller must REMEMBER to populate is not a
mechanism** — the job records' caller-populated `origin` was `{}` in every live record days after it
shipped. Making `--meta` mandatory also points the cost the wrong way: it turns every documented
in-chat invocation into a hard exit-2 refusal on the surface where asking is most valuable,
converting a silent gap into a broken door. And a mandatory free-text field is satisfied by
`--meta '{"source":"me"}'`.

**Kept as the typed half of (b):** an explicit `--origin-session` override, so a caller who knows
better than the environment can say so.

### 4.2 (b) Stamp the caller from the environment — **the recommendation (phase 1)**

The stamp already exists on the sending process. Two environment variables are present and already
load-bearing elsewhere:

| Variable | Set by | Already read by |
|---|---|---|
| `SENESCHAL_SESSION_SOURCE` | the daemon's child-env builder — `"watch"` for a peek, `"daemon"` for the warm spawn, `"scheduled"` otherwise | the Telegram send path, which arms the Watch ack gate |
| `CLAUDE_CODE_SESSION_ID` | the Claude Code CLI, in every session | the job-origin stamp (`origin.session_id`) |

Pre-phase-1, the picker module read **neither**. The asymmetry is the argument: the send path reads
the session source because **a peek is a second sender** (see
[`../modes/watch.md`](../modes/watch.md) and `../references/reminders-policy.md` → *The second
sender*), and prose in a cheap one-shot's instructions cannot hold that — so the gate lives in the
send path. **That fix was applied to one send path.** The picker is a second send path, reached by the
same surface, and had never heard of the interlock.

**Design.**

- `ask()` writes `record["origin"]` **unconditionally**, from the environment and the process:
  `{session_id, source, cwd, pid, stamped_by}`.
- `stamped_by` ∈ `{"env", "flag", "none"}`, matching the job-origin stamp, so a later reader can tell
  whether the auto-stamp fired rather than guessing from an absence.
- `--origin-session` overrides `CLAUDE_CODE_SESSION_ID` (⇒ `"flag"`). Neither present ⇒
  `stamped_by: "none"` and the fields that could not be filled are **absent, never invented**.
- `origin` rides onto the **tombstone** too — the tombstone is what survives the week, and an expired
  unattributed ask is where "who asked this?" is hardest and matters most.
- Fail-open absolutely: a raise anywhere in the stamp costs the **field**, never the record and never
  the send.
- **A census counts `source` presence separately from `stamped_by`** (`list --origin`,
  `origin.stamped`): `stamped_by` scopes to `session_id` alone, so it reads `"none"` on a record that
  *did* name a source — the motivating case's own shape. `origin.stamped` names the parts actually
  captured, and is absent (never back-filled) on records written before it existed.

**Caveat, stated up front.** `CLAUDE_CODE_SESSION_ID` is set by the CLI *over* whatever the parent
passes in, so it cannot join a child back to its parent. That is fine here and arguably the point:
the value a sending process stamps is *its own* session — exactly the actor a reader of a bad
outbound message wants named. Do not reuse it as a parent join.

**Cost.** One small function, one record key, one CLI flag, tests. No change to the send, the
keyboard, the resolve path, the tap, or the gate. `dry_run` returns the stamp so it is inspectable
without sending.

### 4.3 (c) Refuse an ask whose options promise impossible actions — **not as posed**

**A general "can the assistant do this?" checker is not tractable, and none is proposed.** Option text
is free prose written by a model mid-turn; judging whether *"I'll move it and send them a note"* is
within grant requires the sentence, the surface, the grant, and the counterparty. Building that into
the picker would put a second, weaker copy of the autonomy policy inside a picker library — which the
picker's own design forbids (*dispatch lives in the daemon's callback path, or effects accumulate here
and the picker stops being a picker*).

What *is* tractable is narrow, and became phase 3:

- a small **tracked constant** of surfaces the assistant provably cannot reach;
- matched **literally**, case-folded, **against option text only** — never the question body, which
  legitimately names unreachable things when reporting on them;
- **refusing nothing**: one stderr line naming the match, plus a flag on the record for measurement.

---

## 5. Q4 — the capability claim

**An ask that offers to act is a promise.**

### 5.1 Did the repo assert what the assistant cannot reach?

**Partly, and asymmetrically: it enumerated capabilities and their gates, and asserted no absences.**
The positive half is thorough — [`../references/autonomy-policy.md`](../references/autonomy-policy.md)
and the channel map state which sends are ask-high, which doors a calendar read tries, what a bridge
can and cannot do. Every entry is framed as *a capability with a gate*.

The negative half did not exist: a grep for no-access phrasing near work surfaces returned nothing.
Which workspaces, mailboxes, and calendars the owner has that the assistant **cannot** see lived only
in per-session memory — recalled in a full session, not in a cheap one-shot.

**And the failing surface had the channel map open.** It was consulted and had no answer to give.

### 5.2 Enforceable at the ask boundary, or advisory?

**Advisory.** The autonomy policy is explicit that the prose file *is* the gate and that no
supervising process can stop an ask-high action; and **asking is act-low** — a picker grants a
*question*, never an answer. The gate looks at what the send **is**, not at what an option
**promises**. A picker whose options are all ask-high actions is, at the boundary, still just a
message to the owner.

So a check at the ask boundary can only be advisory, and should say so by refusing nothing:

1. **Direction of error.** Not asking is strictly worse than asking badly — the merge guard's own
   rule is that *being unable to ask is strictly worse than a duplicate buzz*. A picker that does not
   arrive is a decision the owner never gets to make.
2. **A literal matcher fails open on every spelling it did not predict.** A refusal resting on one
   would catch the easy half and license confidence in the rest — worse than catching the easy half
   and saying so.

### 5.3 Recommendation (what phase 3 did)

**Ship the assertion before the check.** The higher-value half is writing down, in the file the
failing surface actually reads, what the assistant has no channel to:

- a **"What the assistant cannot reach"** section in
  [`../references/comms-mapping.md`](../references/comms-mapping.md) — unconnected workspaces,
  mailboxes, and calendars are unreachable, and an organisation reached only through another one has
  no channel of its own, so no option may offer to contact it;
- the same fact in [`../modes/watch.md`](../modes/watch.md), since the peek is a cheap one-shot with a
  short context and its own file is where its rules belong *(not yet carried in this tree's
  `watch.md` — the channel map is the canonical statement until it is)*;
- **then** the no-access constant, read at ask time — **warn on stderr, flag the record, refuse
  nothing.**

The order matters: a constant with nothing behind it is a list nobody can argue with, and the flag it
sets is only readable as a finding if the rule it encodes is written somewhere a human agreed to.

---

## 6. Phased recommendation

| Phase | What | Why in this order |
|---|---|---|
| **1** — **BUILT** | **`origin` on the ask record, stamped from the environment.** `{session_id, source, cwd, pid, stamped_by}` on the record **and the tombstone**; `--origin-session` override; fail-open per field. §4.2 | Answers "who asked" for every future ask, including the ones nobody remembers to attribute. Zero behaviour change to the send |
| **2** — **BUILT** | **A landed picker leaves an assertion-log row** — `kind: "question"`, carrying the body, the question id, and phase 1's `origin`, at the send site after the send lands, never-raises | The record of what the assistant has said otherwise excludes every question it has asked. Depends on phase 1 for the field worth carrying |
| **3** — **BUILT** | **The no-access assertion, then the literal check.** Prose into `comms-mapping.md` (+ `watch.md`, pending here); then the constant matched against option text — **stderr warning + record flag, never a refusal.** §5.3 | Independent of 1–2, but lower value alone: without phase 1 a flagged ask still has no author |
| **4** — **OPEN** | **Whether an unattributable ask should ever be refused** — gated on the owner's decision and on phase 1's data | **Do not pre-decide.** §9 |

**Taken with phase 1: the peek launch log names its child — by PID, not session id.** The line reads
`• comms peek launched (pid NNNN)`. The session id is not knowable at the spawn (the CLI mints it
inside the child), so naming it would mean passing `--session-id` and changing how the peek launches —
a behaviour change phase 1 has no business making. The PID does **not** join to a picker's
`origin.pid` (that names the picker process, a grandchild); the join phase 1 provides is
`origin.session_id`, and this line is only the spawn side of the same question.

---

## 7. Non-goals

- **A general capability checker.** §4.3.
- **Refusing any ask.** §5.2's direction-of-error argument holds in every phase that touches the ask
  path.
- **Changing the approval gate.** Asking stays act-low. A tap remains not an approval; the
  `--meta`-carried approval path is untouched.
- **Retro-filling existing records.** Nothing in one says who sent it, so a migration would guess, and
  a wrong guess is worse than a blank. The store self-liquidates in 7 days.
- **Reading the owner's per-session memory from code.** The no-access facts are asserted in the repo
  (or the owner profile) on their own terms, not scraped from a recall mechanism.
- **Anything about `--meta` as it exists.** It stays opaque and uninterpreted; `origin` is a separate,
  non-omittable key beside it.

---

## 8. What this spec does NOT establish

- **Why the peek read the thread as a conflict.** The truncated tail it read and its stated conclusion
  are on record; the step between them is not, and no proposal here depends on it.
- **Whether other meta-less questions came through the same door.** Their shape was consistent with
  hand-authored asks; that is not proof.
- **Why one peek had a session-registry entry and an identical one did not** (§2.1). Observed, not
  diagnosed; a different bug, and phase 1 does not depend on the registry.
- **Whether that peek should have been suppressed at all.** The peek returns early when a live session
  is registered; whether a delegated-job session counts as live for that check was not determined.
- **Any rate.** The question store retains 7 days and harness transcripts are outside the repo's
  control. A rate needs the measurement phase 1 makes possible.

---

## 9. Phase 4 — the open question, stated rather than answered

**This is the owner's, and a builder answering it would be worse than leaving it open.** Phases 1-3
are mechanisms, checkable against the tree. Phase 4 is a **policy about when the assistant may not
speak**, and the harm on this path runs in one direction (§5.2). A rule invented here would be a
decision about that tradeoff made by whoever happened to be holding the file.

### 9.1 The question

> **Should an ask whose author cannot be established be refused, held, or sent with a marker?**

| | What it does | What it costs when it is wrong |
|---|---|---|
| **Send it, as today** | Nothing changes. `origin.stamped_by: "none"` is recorded and countable | An outbound message in the assistant's voice with no author — but now visible in `list` and in the assertion log |
| **Send it, marked** | The picker carries a line: *"I can't tell you which of me sent this."* | Noise on every ask from a surface that legitimately has no session id; the owner reads a caveat they cannot act on |
| **Hold it** | The ask goes to a queue and something else releases it | A decision the owner never gets to make, delayed by a mechanism they cannot see — the failure mode §5.2 names |
| **Refuse it** | Exit non-zero, no send | The same, permanently — and from whichever surfaces turn out to be unattributable, a set nobody has measured |

### 9.2 What would license an answer

**The count of `stamped_by: "none"` — and of `origin.stamped` missing `source` — over a few weeks.**
That says whether an unattributable ask is a rare pathology or the ordinary case for some surface
nobody thought about. Records from before phase 1 are absent by design, not unattributed.

**A near-miss argues for waiting.** §2.1 observed two identical peeks, one registered and one not, and
nobody has diagnosed why. If the same inconsistency reaches `CLAUDE_CODE_SESSION_ID`, a refusal rule
would silently eat some peeks' questions — and the first symptom would be a question that never
arrived, the one failure this path cannot detect.

### 9.3 What is NOT open

- **Whether a flagged (`no_access`) ask is refused.** It is not; settled by §5.2. Phase 3 warns.
- **Whether a tap is an approval.** Unchanged. §7.
