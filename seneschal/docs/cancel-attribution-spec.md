# Cancel attribution and the ack-toned completion push

**Status:** `BUILT` — extended by §12. Shipped: `cancelled_by` + `cancel_reason` on the record,
`--why` / `--cancelled-by-session` on `jobs.py cancel`, the three-rung wording ladder in `notify_text`,
`SENESCHAL_SESSION_SOURCE` from `presence.child_env(source=)` with only the warm spawn passing
`daemon`, and §9.4's non-demotion rule in `sentinel.write_session_heartbeat`. **The push is unchanged:
still unconditional, still exactly once, still stamped only after the send lands.** §12 adds a fourth
field, `cancel_request`, and — for the first time — a real conditional on the separate **wake**
mechanism (never the push itself, which §1's non-negotiable still protects unconditionally): an
assistant surface that cancelled a job on its own initiative no longer wakes the warm session to
re-explain a cancel it already explained in the same turn. · **Scope:** three record fields written by
`jobs.cancel_job` (`cancelled_by`, `cancel_reason`, and since §12 `cancel_request`), CLI flags on
`jobs.py cancel` (`--why`, `--cancelled-by-session`, and since §12
`--requested-by`/`--reason-class`/`--request-turn`), a wording ladder inside `jobs.notify_text`
(short-circuited by §12 for the new case), and — **riding `job-origin-routing-spec.md` phase 0** (§7)
— one env var set in `presence.py` plus one non-demotion rule in the session-registry writer.
**Parent:** `background-jobs-spec.md`. **Depends on:** `job-origin-routing-spec.md` phase 0 (§7 below).

**The problem.** The assistant cancels a job on purpose — a misconfigured launch, killed seconds in —
and the daemon then pushes a completion notification about it, because the push fires on every
terminal state including `cancelled`. That buzz is a small annoyance next to the value of a push that
never goes missing, so **the guarantee wins, and this spec does not relitigate that.** It changes how a
cancel *reads*, and nothing else.

---

## Design decisions

**1 — `cancel` grows a `--why`.** `jobs.py cancel` gets an optional `--why <text>` whose value rides
into the completion push. Absent `--why` must degrade to *exactly* rung 1's flat copy, not to an empty
clause dangling off the end of a sentence. Designed in §4.3 (storage, bounding, escaping) and §5 (the
exact wording, with and without).

**2 — one assistant, several jobs.** The rung boundary is **an assistant surface vs. unattributed** —
*not* this-exact-session vs. another-session. A cancel from the warm daemon session **and** a cancel
from a desktop `/assistant` both speak in the first person, because to the owner they are one
assistant managing several jobs at once. The alternative — a desktop `/assistant` saying "that was not
me, that was another instance" — is recorded as rejected in §9.2.

**2a — delegated sessions are not the "I".** A delegated build/agent session that cancels a job speaks
at rung 2, naming checkout and branch. So the "I" of decision 2 is **the surfaces the owner is present
at** — the warm daemon session and a desktop `/assistant` — and a session the assistant *dispatched*
speaks at rung 2. `ASSISTANT_SURFACES` (§5) therefore does **not** contain `"build"` (§11.1).

**3 — `SENESCHAL_SESSION_SOURCE` lands *with* job-origin phase 0**, not separately, because it repairs
`origin.source` too (§7).

**Why tone at all, if cancels are rare.** Frequency is not the argument — a cancel is a minority of
pushes, and small volume would not justify work on its own. The case is **a cancel the owner asked for
reading as an ALERT when it should read as an acknowledgement** — a correctness-of-tone argument that
holds at one cancel a month.

**None of these touches the rail below.** No decision adds a branch to *"does it fire?"*.

## 1. The one non-negotiable

**The push stays unconditional. Nothing here may add a branch to "does it fire?"**

The parent spec's invariant is *no silent path* — every job reaches a terminal state and every terminal
state notifies exactly once (`background-jobs-spec.md` §2). Every conditional added to a delivery path
is a place a real completion can silently go missing, and a lost real completion costs incomparably
more than a redundant cancel notification. The parent's `state/metrics.jsonl` cautionary tale is about
exactly this class of erosion: a contract that lived only where it was already being honoured, and so
produced zero rows in a month.

**The fix is tone, not suppression.** A `cancelled` job still pushes, still pushes exactly once, and is
still stamped only after the send actually lands. It just gets to *read* as an acknowledgement rather
than an alert. Anyone reading this later and finding an opt-out here has found a bug, not a feature.

The rail binds the additions too: `--why` (§4.3) changes what the push *says*, never whether it fires;
an unreadable, malformed, or oversized reason degrades the wording and nothing else (§5, §8 case 3).

## 2. What was true before this shipped

| Fact | Where |
|---|---|
| The completion push fires off the record, unconditionally, once per terminal state | `jobs.reconcile` |
| `cancelled` is a terminal state like any other | `jobs.TERMINAL` |
| Its push text was a single hard-coded line: `🛑 "{title}" cancelled after {dur}.` | `jobs.notify_text` |
| `cancel_job` killed both pids, stamped `cancelled`/`ended_at`, saved. **It recorded nothing about who called it, or why** | `jobs.cancel_job` |
| The **only** cancel path is the `jobs.py cancel <id>` CLI | `jobs.py` argparse |
| The cockpit has **no** cancel route, by decision | `cockpit-spec.md` Jobs panel; `cockpit/server/jobs.py` |
| The record round-trips unknown keys: `save_job` writes the whole dict, `load_job` requires only `id` | `jobs.save_job` / `jobs.load_job` |
| Record schema is `seneschal.job/1` | `jobs.py` |
| `notify_text` is called *inside* `reconcile`'s per-record `try` | `jobs.reconcile` |
| The push text reaches Telegram as **argv, then a urlencoded form body** — never a shell | `sentinel.send_telegram` (`subprocess.run([...])`, no `shell=True`) → `telegram_send.py` |
| `parse_mode` defaults to **empty**, i.e. plain text | `telegram_send.py` |

In practice nearly every cancel is deliberate — a relaunch after a bad start, a spawn test, a job
superseded mid-flight — and **a cancel the owner asked for arrived sounding like an alarm**, the same
siren tone as a job that died on its own. That is wrong at any frequency.

## 3. Who can cancel, and how that should read

Decision 2 sets the boundary: **an assistant surface speaks in the first person; anything else is
either attributed or unattributed.** Attribution matters mainly insofar as it predicts **whether the
recipient has already been told** — identity for its own sake buys nothing at 3 a.m. — but the *voice*
is settled by decision 2 rather than by instance identity.

| Canceller | Voice | Why |
|---|---|---|
| The daemon's **warm session** (the assistant, mid-conversation) | **First person** — rung 1 | The cancel happened in a turn that speaks to the owner on the same channel the push lands on. Saying so *is* the report; the push arrives seconds later saying it again. |
| A **desktop `/assistant`** session | **First person** — rung 1 | **Decision 2.** Different process, same assistant. To the owner there is one assistant managing several jobs at once, and hair-splitting about which instance held the knife reads as pedantry, not honesty. |
| A **build / delegated agent** session | Attributed — rung 2 (§11.1) | Something the assistant *dispatched*, not the assistant speaking. The owner may not be watching it at all, and the useful fact is which checkout and branch. |
| A **scheduled** run | Attributed — rung 2 | Nobody was in the room; there is a session to name but no one to speak as. |
| **The owner at a terminal** | Unattributed — rung 3 | A bare shell exports no session id, so this is undetectable by construction. Rung 3 is the pre-existing wording. |

The line that matters is therefore **assistant surface vs. attributed-other vs. unknown**. Which
sources count as an assistant surface is one named constant (§5), so adding a future surface is a
one-line change in one place — and the mechanism that makes *desktop* distinguishable from *build* at
all is §9.4, which is the load-bearing prerequisite for this table.

## 4. The design

### 4.1 Two new fields: `cancelled_by` and `cancel_reason`

```json
"cancelled_by": {
  "session_id": "00000000-0000-0000-0000-000000000000",
  "source": "daemon",
  "cwd": "C:/.../seneschal-dev",
  "branch": "feat/whatever",
  "stamped_by": "env"
},
"cancel_reason": "misconfigured launch — wrong branch"
```

**Why a new top-level field and not `origin`.** They are different actors. `origin` is the address of
the session that *started* the job; a cancel is very often performed by a different session entirely
(job started by a warm turn, killed hours later from a build session), and folding the two together
would make `origin` mean "whoever touched this last" — destroying the return-address property phase 0
exists to build. **Reuse the mechanism, not the field.**

`cancelled_by`'s fields are `origin`'s (§3.2 of the origin spec) minus `goal`, which is a property of
starting work and meaningless for stopping it. `stamped_by` is `env` / `flag` / absent — the same cheap
provenance, and the same reason: weeks in, it is how you find out whether the stamp actually fires.

**Why `cancel_reason` is a sibling and not `cancelled_by.why`.** Because the two have independent
presence and a field named "by" holding a reason is a naming lie that every future reader trips on. A
bare-terminal cancel with `--why` has a reason and **no** actor; a warm-session cancel typed without
`--why` has an actor and **no** reason. Nesting the reason under the actor would force one to
materialise the other, and would make "`cancelled_by` absent, `{}`, and malformed are all the legacy
behaviour" (§6) newly false the moment someone passed a reason from an unattributable shell.
`cancel_reason` is a **plain string** — never a dict, never a list — and a reader that finds anything
else treats it as absent.

Both are written by `cancel_job`, once, at cancel time. Both are absent on every record that predates
them, and `cancelled_by` is absent on every cancel that cannot be attributed — **absent and `{}` must
be treated identically**, which is §5 rung 3, which is the legacy behaviour exactly.

`cancel_job` returns an already-terminal record untouched. A `--why` on a job that already finished
therefore writes **nothing** and pushes nothing, because there is no un-notified terminal transition
left to describe. That is correct and it is worth one sentence here so nobody "fixes" it later.

### 4.2 Where the values come from

**The identity: phase 0's resolver, unchanged.** `--cancelled-by-session <id>` (explicit, wins) →
`$CLAUDE_CODE_SESSION_ID` (auto, the default path) → absent. This is `job-origin-routing-spec.md` §3.1
verbatim; do not invent a second addressing scheme. Auto is primary for the same reason it is there: a
flag nobody passes is `metrics.jsonl` again, and `jobs.py cancel` is typed by a model mid-turn.

**The `source`: one new env var, because the registry cannot answer this.** §9.1 is the finding in
full. In short: the session registry is keyed by `CLAUDE_CODE_SESSION_ID` only for **hook-stamped
`build` entries**; the daemon's own entry is the literal `daemon.json` (the daemon's heartbeat passes
no session id, so `sentinel._session_id_for` falls back to the source) and the desktop entry is
`desktop.json` the same way. So a registry lookup by UUID can return `build` or nothing, and **never
`daemon`** — the one value this whole spec turns on.

The fix is a few lines in `presence.py`, in the place that already exists for exactly this kind of
child-env decision:

- `child_env()` takes a `source` parameter defaulting to `"scheduled"` and sets
  `SENESCHAL_SESSION_SOURCE` in the returned env.
- The **warm-session spawn only** passes `"daemon"`. The headless one-shots (Watch, reminders,
  scheduled runs) keep the default — putting the marker unconditionally in `child_env()` would label a
  Watch peek as the warm session, which is the bug this paragraph exists to avoid.

**And, because of decision 2, one more: the desktop `/assistant` surface must become resolvable.** The
env var cannot reach it — a slash command runs *inside* a session it did not spawn, and Claude Code's
Bash calls do not share shell state — so the desktop marker has to live in the registry under the
harness session id. §9.4 is that finding and its one-line mechanism (an assistant-surface source is
never demoted to `build` by a later hook stamp). **Without §9.4 there is no rung 1 for desktop**, and
decision 2 is unimplementable; with it, the resolution below is uniform.

Resolution order for `source`, then: `SENESCHAL_SESSION_SOURCE` → the registry entry for `session_id`
when one exists → absent. `cwd`/`branch` come from that registry entry when it exists and are
enrichment only, never identity.

**Everything here is fail-open.** An unreadable env, a missing registry, a corrupt entry — each yields
a smaller `cancelled_by`, at worst an absent one, which is rung 3. **A resolver that throws must never
prevent the cancel**: the whole resolve sits inside its own `try`, and the kill runs whatever it does.
Losing attribution is a cosmetic failure; failing to cancel is a real one.

**Claim before kill** (`background-jobs-spec.md` §3.10). An earlier order — kill and stamp first,
attribute after — was a bug: killing first woke the shim onto the same unlocked record, and a cancel
could come back `failed`, exit 1, **with `cancelled_by` erased** — this feature's whole output, gone,
on the path it was built for. `cancel_job` now stamps, attributes, and **saves the claim to disk BEFORE
it kills**, then re-asserts the save afterwards. Every property above is preserved: the resolve is still
in its own `try`, a raise there still costs only the attribution, and the claim's own write is guarded
too so an unwritable record cannot cost the kill.

### 4.3 `--why`: how an operator-typed string reaches a Telegram send safely

**Decision 1.** `jobs.py cancel <id> [--why <text>]`. Optional, no default, and the *only* new argument
on the only cancel surface there is (§9.3).

**Stored normalised, at the write, once.** `cancel_job` does not keep the raw string. It applies, in
order:

1. **Reject the non-strings.** Anything that is not a `str` (or is absent) → no `cancel_reason` key at
   all. There is no coercion; `str(x)` of a stray object is how `<object at 0x…>` ends up in a push.
2. **Flatten to one line.** Every character in Unicode categories `Cc`/`Cf` (which covers `\n`, `\r`,
   `\t`, and the bidi/zero-width controls) plus every run of whitespace collapses to a single space,
   then strip. **The head line must stay one line**: `notify_text` joins its parts with `\n`, so an
   embedded newline would not merely wrap — it would make the tail of the reason indistinguishable from
   a `retry_note` or a `Last line:` part. A pasted multi-line log is the realistic accident here, not an
   attacker.
3. **Bound it.** `_WHY_CHARS = 200`, matching `_TAIL_CHARS` because the log tail is the existing
   precedent for "free text quoted into a push". Longer values become `text[:199] + "…"`, so the stored
   value is never more than 200 characters. This keeps the head far below Telegram's 4096-character
   message limit, where a rejected `sendMessage` would be a *delivery* failure, not a cosmetic one (see
   below).
4. **Empty is absent.** If the result is `""` after normalisation — `--why ""`, `--why "   "`, `--why`
   with a value of only control characters — **no key is written**, and the push is byte-identical to
   the no-`--why` case. This is decision 1's explicit requirement: absent degrades to exactly the flat
   copy, never to a dangling `: ` clause.

Storing the normalised form (rather than normalising at render) means every reader is safe by
construction — `jobs.py status`, a future cockpit Jobs-panel field, a human `cat`ting the JSON — and
there is exactly one place to audit. Renderers still re-check the type (rule 1) because a record can
be hand-edited and `load_job` validates nothing but `id`.

**Escaping: what is actually exposed, and what is not.**

- **No shell, ever.** `sentinel.send_telegram` builds an argv **list** for `telegram_send.py` with no
  `shell=True`, and `telegram_send.py` urlencodes the POST body. Quotes, `;`, `$`, backticks and
  newlines are inert on that path, which already carries arbitrary log-tail bytes.
- **The one live hazard is `parse_mode`.** It defaults to `""` → sent as plain text, and under that
  default no escaping is needed at all. If a future config sets `MarkdownV2` or `HTML`, an unbalanced
  `*`, `_`, `[` or `<` in **any** interpolated field makes Telegram reject the whole `sendMessage` with a
  400 → the send returns `False` → `notified_at` is never stamped → the tick retries forever and the job
  **never notifies**. That is the no-silent-path rail failing, not a typo.
  **Therefore:** `--why` is inserted as data and is **never** wrapped in backticks, asterisks or any
  other markup by this feature; and if a parse mode is ever configured, the escape belongs in
  `telegram_send.py` covering *every* field, because `title` and the log tail have the identical
  exposure and neither is bounded. This spec does not add a per-field escape that would cover one field
  and quietly leave the other two.
- **There is no attacker in this threat model.** The only writers of `--why` are the assistant
  (mid-turn) and the owner (at a shell). "Hostile input" here means an accidentally pasted 40 KB blob or
  a multi-line log fragment, and rules 2–4 are sized for exactly that.

## 5. The wording ladder

Evaluated in `notify_text`, on `status == CANCELLED` only.

Rung selection reads one named constant — `ASSISTANT_SURFACES = {"daemon", "desktop"}` (decision 2).
Adding a future first-person surface is a one-line change there and nowhere else.

1. **First person — an assistant surface.** `cancelled_by.source in ASSISTANT_SURFACES`: the warm
   daemon session or a desktop `/assistant`, which under decision 2 are the same "I".
   - without `--why`: `🛑 "{title}" — stopped, {dur} in. That was me.`
   - with `--why`: `🛑 "{title}" — stopped, {dur} in. That was me: {why}`
2. **Attributed, not an assistant surface** — a `session_id` resolved, `source` outside
   `ASSISTANT_SURFACES` (a build/delegated agent, a scheduled run, an unknown source):
   - without: `🛑 "{title}" cancelled after {dur} — from {cwd_basename} @ {branch}.`
   - with: `🛑 "{title}" cancelled after {dur} — from {cwd_basename} @ {branch}: {why}`
   - …falling back to `— from another session ({source}).` / `— from another session ({source}): {why}`
     when the registry gave no `cwd`/`branch`. The copy leans on the checkout and branch rather than the
     source word deliberately: outside the daemon, `source` is best-effort and reads `build` for nearly
     everything (§9.1). The checkout is the part that is both true and useful.
3. **No attribution** — `cancelled_by` absent, `{}`, not a dict, or carrying no `session_id`:
   - without: **the legacy string**, byte for byte: `🛑 "{title}" cancelled after {dur}.`
   - with: `🛑 "{title}" cancelled after {dur} — {why}`

**One rule for the reason clause, applied at whichever rung is chosen:** it replaces that rung's
terminal period and stays *inside the head line*. A reason is worth reading first — Telegram's
notification preview truncates, and "why" is the part that turns an alert into an acknowledgement — so
it must not become a fourth `\n`-joined part below the fold. If the normalised reason does not already
end in `.`, `!`, `?` or `…`, a period is appended, so `--why "wrong branch"` renders
`That was me: wrong branch.` rather than trailing off.

**A reason clause is available at every rung.** The flag lives on `cancel`, not on a rung, and a reason
typed at a bare terminal (rung 3, the one case that is undetectable by construction) is the case where
the record most needs to say why. It costs nothing: with no `--why` — every legacy record and every
caller that never opts in — rungs 2 and 3 are unchanged and rung 3 is byte-identical to the legacy
string.

Rung 3-without is the default, not the exception: every record that predates attribution lands there,
and so does every future caller that stamps nothing. **Unknown attribution degrades to the legacy
wording, never to a guess** — the push must never say "that was me" about something it cannot show was.

Everything after the head line is untouched: `retry_note`, the log tail, the wake line all still
append exactly as before.

**On `timed-out` and `ended-unknown`: no.** The argument for softening a cancel is that a deliberate act
by a known actor has already been reported by the act of performing it. Neither of those states has an
actor — a deadline elapsing and a process vanishing are things that *happened to* the job, nobody chose
them, and nobody has told the owner anything. They are precisely the states where an alert tone is
correct, and `ended-unknown`'s wording in particular is carefully honest about not knowing. Leave both
alone.

## 6. Backward compatibility

- Records that predate this carry neither `cancelled_by` nor `cancel_reason`; their cancels are rung
  3-without, i.e. unchanged, byte for byte.
- **The schema stays `seneschal.job/1`** — these are additive keys on a record whose writer has no key
  whitelist and whose reader requires only `id`. Bumping it would force every reader to care about a
  change none of them can see.
- `cockpit/server/jobs.py` never reads either and does not need to; it is a deliberately independent
  reader of the on-disk shape (`cockpit-spec.md`). Same conclusion the parent spec reached for
  `attempts[]` (§7.4.1) and the origin spec reached for `origin` (§3.4). If it ever *does* render the
  reason, §4.3's store-normalised discipline means it inherits the bounding for free.
- **The rule for every future reader:** absent, `{}`, wrong-typed and malformed are the same thing, and
  that thing is the legacy behaviour. For `cancel_reason` specifically, "wrong-typed" includes any
  non-`str`.

## 7. Dependency on the origin work

**`SENESCHAL_SESSION_SOURCE` lands *with* `job-origin-routing-spec.md` phase 0, not separately,**
because it repairs `origin.source` too — §9.1 is a finding against phase 0 as specified, not only
against this spec, and splitting the fix across two changes would mean shipping phase 0 with a `source`
that reads `build` for the warm session and then correcting it later. The env-var change (and the
registry non-demotion of §9.4, which is the same defect for the desktop surface) therefore **rides
phase 0**.

This spec was written against phase 0's *specified* shape — the `--origin-session` /
`$CLAUDE_CODE_SESSION_ID` resolver and the registry enrichment — so **reconciling it against phase 0's
landed code is the implementer's first task**, before writing a line.

The coupling is small and one-directional: this feature needs phase 0's *session-id resolver*, roughly
three lines of env read. If phase 0 slips, build the resolver here — but **build one, shared, in
`jobs.py`**, never a second copy alongside phase 0's. Two resolvers that drift is how `origin.source`
and `cancelled_by.source` end up disagreeing about the same session.

## 8. The tests that matter

In `seneschal/scripts/test_jobs.py`, alongside `test_every_terminal_path_notifies_exactly_once` and
`test_cancel_kills_both_pids_and_still_notifies`, which stay green unchanged.

**The regression pin, named explicitly — the same shape as the origin spec's routing pin:** *the push
still fires exactly once on `cancelled`* —

1. **with an assistant-surface attribution** (`cancelled_by.source == "daemon"`, and again
   `== "desktop"` — decision 2's two sources must be pinned to the *same* first-person string, because
   that equality is the decision) — one push, ack wording;
2. **without attribution** (no `cancelled_by`, i.e. every legacy record) — one push, and the text is
   asserted **byte-identical to the legacy string**;
3. **when the attribution lookup itself throws** — a `cancelled_by` that is a string, a `None`, a dict
   of the wrong shape; a `cancel_reason` that is a dict, a list, an `int` — one push, correct rung, no
   exception escaping `notify_text`.

Case 3 is the load-bearing one and deserves its reason in the test's docstring: `notify_text` is called
*inside* `reconcile`'s per-record `try`. A raise there is caught, logged, and `notified_at` is never
stamped — so the next tick retries, raises again, and the job **never notifies at all**. A cosmetic
feature would have silently converted the no-silent-path guarantee into a permanent silent path for
that record. That is the whole risk surface of this change, and `--why` widens it by one more field of
operator-typed input, which is why the malformed-reason variants belong in the same test.

**`--why` gets its own four, all cheap** (decision 1's requirements, made falsifiable):

- `--why ""` and `--why "   "` write **no** `cancel_reason`, and the push is byte-identical to the
  no-flag case at every rung — the "must not dangle an empty clause" requirement;
- a 5 KB `--why` is stored at exactly 200 characters ending `…`, and the rendered head stays one line;
- a `--why` containing `\n` / `\r` / `\t` renders as one line, and `notify_text`'s part count is
  unchanged (this is the assertion that actually catches a regression — a stray newline would otherwise
  look fine in a bare string compare);
- the terminal-punctuation rule: `--why "wrong branch"` ends `.`, `--why "wrong branch."` is not
  double-punctuated.

Plus three unit tests on the writer: `cancel_job` stamps `cancelled_by` from the env; `cancel_job`
still cancels when the resolver raises; and `cancel_job` on an **already-terminal** job writes neither
field and changes nothing (§4.1's last paragraph).

## 9. Where reading the code contradicted the first design

### 9.1 The session registry cannot identify the daemon's warm session — and mislabels the desktop one

The origin spec's addressing design has `source` "copied from the registry entry when one exists."
Reading the registry says that lookup can essentially never succeed for the two interactive sources:

- The daemon writes its entry as `write_session_heartbeat(state_dir, "daemon", …)` with **no
  `session_id`**, so `sentinel._session_id_for` falls back to the source and the file is `daemon.json`
  with `"session_id": "daemon"`.
- `.claude/commands/assistant.md` invokes `session_heartbeat.py --source desktop` the same way, which
  writes `desktop.json`.
- Every *other* entry is UUID-named and hook-stamped `source: "build"` — `session_stamp.py` writes
  `"build"` for **every** session on the box, explicitly including the daemon's own spawned runs. These
  are the large majority of live entries.

So a lookup keyed on `CLAUDE_CODE_SESSION_ID` returns a `build` entry or nothing. That is fine for
`cwd`/`branch` enrichment — which is where it's most useful anyway (§5 rung 2) — but it means:

- `origin.source` as specified would read `build` for a warm turn, a desktop `/assistant`, *and* a
  build session alike. **This is a finding against phase 0 as specified, not only against this spec**,
  and it is cheap to fix in the same place: the `SENESCHAL_SESSION_SOURCE` marker of §4.2 gives phase 0
  a correct `source` for free. Decision 3 is exactly this: fix both together.
- Rung 2's copy must not put weight on the source word, since an unfixed desktop `/assistant` reads
  `build`. §5 already accounts for this.

### 9.2 The rejected alternative: per-instance voice

A first draft argued that a desktop `/assistant` cancel should get rung 2, not rung 1, because
"claiming 'that was me' would not be honest — it was a different instance." Decision 2 rejects it. The
instance boundary is an implementation fact about how the assistant is hosted; it is not a fact about
who is assisting the owner. A push that hedges about which process held the knife is not more honest,
it is just more granular — and granularity nobody asked for reads as evasion. §3 and §5 implement
decision 2; the rejected argument is recorded only here, so no future reader mistakes a leftover for
current design.

What survives from it is the *narrower* claim — that something the assistant **dispatched** is not the
assistant **speaking** — which is §11.1.

### 9.3 The cancel surface is narrower than it looks

There is exactly one way to cancel — the CLI. The cockpit deliberately ships no cancel route. So the
attribution stamp and the `--why` flag have **one** call site, and there is no second surface that could
produce an unattributed cancel by construction. That is why §4.2's resolver needs no plugin architecture
and why `--cancelled-by-session` is only for tests and hand-runs.

### 9.4 Decision 2 needs a mechanism: desktop is indistinguishable from build

**This is the finding decision 2 turns on.** Decision 2 says a desktop `/assistant` speaks in the first
person; §11.1 says a delegated build session does not. Without more, those two are the **same value**
to the resolver: both are hook-stamped `build` under their harness UUID (§9.1), and the `desktop.json`
entry is keyed on the literal string `desktop`, so it cannot be reached from a session id. The env var
of §4.2 cannot help — a slash command runs inside a session it did not spawn, and Claude Code's Bash
calls do not persist shell state, so `/assistant` can never set `SENESCHAL_SESSION_SOURCE` for itself.

**The mechanism:** make an assistant-surface source **non-demotable** for a given session id.

- The `/assistant` command's registry step passes `--session-id "$CLAUDE_CODE_SESSION_ID"`, so the
  desktop chat stamps a UUID-keyed entry with `source: "desktop"`.
- `sentinel.write_session_heartbeat` gains one rule: **if the previous entry for this id has a source in
  `ASSISTANT_SURFACES` and the incoming source is `"build"`, keep the previous source.** Without it, the
  machine-wide hook — which fires on `UserPromptSubmit` and `Stop`, i.e. twice a turn — would overwrite
  the desktop marker within seconds, every turn.

Consequences, stated so they are chosen rather than discovered: a session that runs `/assistant` and is
later used for build work stays labelled `desktop` until it ends (the entry is deleted on `SessionEnd`
by `session_stamp.py`). That is acceptable and arguably correct — it *is* an assistant surface, and the
only thing the label decides is a pronoun.

`sentinel` keeps its own copy of `ASSISTANT_SURFACES` rather than importing `jobs`'s — `jobs` imports
`sentinel` (deferred, inside a function) and an import back at module level would be a cycle. Two
names, one meaning, the same posture as the cockpit's copy of the governor rank table.

A single invocation carrying `--session-id` is sufficient: the daemon's nudge-deferral gate
(`sentinel.session_is_live`) iterates every entry and matches on `source in GATING_SOURCES` — the
filename is never consulted — so a second, literal `desktop.json` write would be pure duplication.

The chain, end to end:

| Link | Result |
|---|---|
| the command writes a UUID-keyed entry | `<uuid>.json`, `source: "desktop"` |
| a later `build` stamp (the hook, twice a turn) | source stays `desktop` — the non-demotion rule holds |
| `jobs.build_cancelled_by` resolves it | `{"source": "desktop", …, "stamped_by": "flag"}` |
| the rendered push | `🛑 "transcribe" — stopped, 22s in. That was me: misconfigured launch.` |

**Fallback if the rule is judged too invasive for a shared machine-wide hook:** `cancel` also accepts
an explicit `--cancelled-by-source`, and `/assistant`'s Chat-mode instructions pass `desktop` when
cancelling. It is prompt-side and therefore weak — the `metrics.jsonl` failure mode — but the
degradation is benign: a forgotten flag lands at rung 2, naming the checkout, not at a wrong first
person.

This rides phase 0 with the env var (§7): it is the same defect, in the same file family, for the other
interactive surface.

## 10. Out of scope, stated so it stays that way

- **No suppression.** Not for cancels, not for anything. §1.
- **No digest, no batching, no quiet hours.** Delivery timing is untouched; this spec edits one string.
- **No new state file.** The record already round-trips unknown keys.
- **No cockpit cancel route.** Out of scope here and already decided elsewhere.
- **No wording change to `timed-out` / `ended-unknown` / `failed` / `done`.** §5.
- **No `cancelled_by` or `cancel_reason` on non-cancel terminal states.** There is no actor to name and
  nobody typed a reason.
- **No per-field Markdown/HTML escaping.** Under the plain-text `parse_mode` default it buys nothing,
  and under a configured one it would cover `cancel_reason` while leaving `title` and the log tail
  exposed — the fix belongs in `telegram_send.py`, for every field, as its own change (§4.3).

## 11. Settled sub-questions

### 11.1 A delegated build/agent session that cancels a job

**Settled: rung 2, naming the checkout and branch.** `ASSISTANT_SURFACES` does **not** contain
`"build"`, and §9.4 has to land before the distinction is even drawable.

A job-spawned `claude` — an agent working a branch, stamped `build` by `session_stamp.py` — cancels
some other job. Decision 2 collapsed *warm daemon* and *desktop `/assistant`* into one first-person
"I". A delegated session does not fall inside it. The reasoning is the one part of the rejected §9.2
argument decision 2 did not touch: a delegated session is something the assistant **dispatched**, not
the assistant **speaking**. The owner is present for a warm turn and present at a `/assistant`
keyboard; they are very often *not* watching a build agent, so a cancel from one is news, and "that was
me" would flatten a thing they should look at into a thing they have already been told. The useful fact
there is *which checkout, which branch* — which is exactly rung 2.

It is one entry in one set to change if revisited: adding `"build"` to `ASSISTANT_SURFACES` (§5) makes
delegated sessions first-person, and nothing else in the design moves.

### 11.2 Worth a glance in use

Whether rung 1's `That was me: {why}` wants any softer connective (`That was me — {why}`) is a
one-character question best answered by reading a few real pushes, not by arguing about it in advance.

## 12. The wake, not just the push

**The problem.** The assistant cancels jobs on purpose and explains each cancel to the owner in the
same turn that made it. The daemon's completion shim then wakes the warm session once per job, each
carrying `wake_text`'s "read the log, then tell the owner plainly what happened" — so the owner hears
the same reason twice, once per cancelled job. Where the shim can tell a self-initiated cancel from one
the owner asked for, it should not wake; where it cannot tell, the woken session should check whether
it already explained, and stay silent if so.

**What §1–§11 already got right, and what they never touched.** The push is a different mechanism
from the wake (`background-jobs-spec.md` §3.5): the push is `notify_text`, fires unconditionally, and
§1's non-negotiable — *nothing here may add a branch to "does it fire?"* — binds it exactly as before.
The wake is a *separate*, later addition (`--wake` / `default_wake`) that additionally hands the job
back to the warm session to compose a second, spoken report. §1 never addressed it because it didn't
exist when §1 was written. This section is entirely about the wake and the "Reading it properly now."
line `notify_text` appends only when a wake is coming — never about whether the push itself fires.

### 12.1 The differentiator: `cancel_request`, built the same way `origin.request` is

`jobs.py cancel` gains `--requested-by {owner,assistant}` / `--reason-class` / `--request-turn`, wired
into `build_request` **exactly as `start` already wires them into `origin.request`** — same resolver,
same fallback to the daemon's open-turn pointer, same refusal to guess (`unresolved`) when nothing
verifies. The result is stored as a new field, `cancel_request`, sibling to `cancelled_by` and
`cancel_reason` and owned by `cancel_job` the same way (§4.1's fail-open-attribution-is-cosmetic
posture, unchanged).

**Why a new field rather than folding into `cancelled_by`.** `cancelled_by` says WHO pressed the
button; `cancel_request` says WHOSE IDEA IT WAS. They can disagree — the warm daemon session presses
the button either way, but sometimes because the owner just asked it to (a picker tap, a message) and
sometimes because the assistant decided on its own mid-turn. Collapsing the two would make it
impossible to tell "the owner already knows, because they asked" apart from "the owner doesn't know
yet, because the assistant just decided this."

### 12.2 `jobs.cancel_wake_suppressed` — the one predicate both the push and the wake read

True exactly when **all** of:

1. `cancelled_by.session_id` is present and `cancelled_by.source in ASSISTANT_SURFACES` (rung 1 — the
   warm daemon session or a desktop `/assistant`, decision 2's singular first-person "I"), **and**
2. `cancel_request.by != "owner"` — the owner did not ask for this cancel.

False for everything else:

- **`cancel_request.by == "owner"`** — a picker tap or a message asked for this. A confirmation is the
  right read here, not a suppression: this is the one cancel the owner is actually waiting to hear back
  on, and the fuller rung-1 wording (`"That was me{: why}."`) plus the ordinary wake still fire,
  unchanged.
- **rung 2** (a delegated/build session) — something the assistant *dispatched*, not the assistant
  *speaking* (§3, decision 2a). The owner may not have been watching it at all, so a cancel from one is
  news, not a repeat, regardless of `cancel_request`.
- **rung 3** (no session attribution at all) — the shim genuinely cannot tell. See §12.4.
- **any non-`cancelled` status** — a job that died had no actor and nobody has told the owner anything
  yet; §5's closing paragraph ("no" to softening `timed-out`/`ended-unknown`) already covered this
  ground and nothing here revisits it.

### 12.3 What changes when it's true

**The push.** `_cancelled_head` returns one short line instead of the rung-1 wording:
`🛑 "{title}" — cancelled by {assistant} — {reason or "no reason recorded"}` — no duration, no "That
was me" (that phrasing is reserved for the owner-requested case, where the fuller confirmation is
correct). `notify_text` also skips every trailing part that exists to restate or point at more detail
(`contradiction_note`, `retry_note`, `completion_note`, the resume-mismatch note, the log tail, and —
this is the fix — "Reading it properly now." / "Log: …") **except the worktree leak note**, which
still appends if present: a leak is actionable information about *this* record, not a restatement of
why the job was cancelled, and dropping it would silently hide it. **The push still fires, still
exactly once** — nothing here touches §1's non-negotiable, because a shorter string is still a string.

**The wake.** `presence.job_wakes` (the pure function the daemon's job-reconcile wake list is built
by, so it's unit-testable without the daemon's async plumbing) drops the record from the wake list
entirely via `cancel_wake_suppressed`. No synthetic inbound is enqueued, so the warm session is never
handed the job to re-read and re-report.

### 12.4 The fallback: rung 3 gets one more sentence instead of a guess

When `cancelled_by` carries no `session_id` at all — a hand-run `jobs.py cancel` from a bare shell, or
a record written before this shipped — the shim cannot answer "did the session that's about to be
woken already explain this?" It does not guess either way. Instead, `wake_text` appends one clause to
the ordinary `[job finished: …]` wake line, **only for this case**: *if you cancelled this job yourself
and already told the owner why, say nothing (no message).*

This hands the question to the one place that can actually answer it — the woken session's own
transcript — rather than the shim answering it wrong in either direction. A rung-1/2 cancel (session
known) never gets this clause: rung 1 is suppressed outright (never reaches `wake_text` with `wake`
still true) and rung 2's/the owner-requested wake is already known to be warranted, so appending a
hedge there would just be noise.

### 12.5 What did NOT change

- §1's non-negotiable — the push is still unconditional, still exactly once, still tone-not-suppression
  for itself. §12 adds a conditional to the *wake*, a mechanism §1 predates and never governed.
- The `--analyze` rule (§10's "no `cancelled_by`/`cancel_reason` on non-cancel terminal states" and
  `background-jobs-spec.md`'s `ANALYSABLE`) — a cancelled job is still never analysed;
  `cancel_wake_suppressed` is never consulted on that path because `ANALYSABLE` excludes `CANCELLED`
  before it would matter.
- Rung 2 and rung 3's push wording (§5) — byte-identical to before this section, in every case that
  isn't `cancel_wake_suppressed`.

## Router entry

**Status:** BUILT; extended by §12.

**What it decides:** Why a purposeful cancel still buzzes, and the three-rung wording ladder that fixes
tone rather than suppressing the push. §12: an assistant-surface cancel the owner didn't ask for also
skips the WAKE, since the push already carries the acknowledgement and the turn that cancelled it
already explained why.
