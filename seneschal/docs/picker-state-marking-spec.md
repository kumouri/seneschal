# Picker state marking — a reaction on the picker, not a sentence after it

**Status:** `PARTIAL(telegram_ask.py's primitives BUILT — mark/setMessageReaction, settle-in-chat, unsettle, and the twin-settle hook in resolve; the marking pass picker_mark.py, picker_retire.py, merge_guard.ask_identity/twin_settlements and the pr_sweep wiring arrive with the PR-sweep port)`
— the design below describes the whole system; in this repository its Telegram half ships first. `telegram_ask.resolve` already
calls `merge_guard.twin_settlements` behind an optional import, so the twin settle (§10.6.1) switches
on by itself the day `merge_guard` lands. Of §10.5's three follow-ons, two are part of the design
(§10.6) and the third — widening the sweep's watched repositories for *asking* — is **declined by
decision**, not backlog. **§6 carries an amendment (§6.1)**: 🔥 no longer names a single winner; it
means *spendable now*, with no uniqueness.

**The owner's idea.** When there is an order the owner should merge pickers in, or a picker has gone
stale, the assistant should **react on the picker itself** — priority as keycap numbers (`0️⃣1️⃣`),
and "stale" as 🛏 (*it got put to bed*). Everything below is that idea. Two of the three glyphs turn
out to be impossible (§3) and the constraint improved the design rather than compromising it (§4) —
but the mechanism, the channel and the reason are the owner's.

---

## 1. The failure this fixes

The motivating day: **three approval taps spent on pull requests that could not merge.** Each tap was
a decision the owner made, held in their head, and carried out; each one bought nothing. In every
case the assistant *knew*, and said so in **prose, after the tap**.

The three were not one class, and the difference is the whole of §5:

| Picker | State when tapped | Class |
|---|---|---|
| A | `MERGED` | the pull request stopped being open |
| B | `MERGED` | the pull request stopped being open |
| C | `OPEN`, `mergeable: CONFLICTING` | the pull request cannot merge as it stands |

**The instrument was wrong, not the content.** A prose warning arrives in the conversation; the tap
happens on the picker. Those are two different places, and the owner is looking at the second one
when they decide. **A reaction is in the same place they are looking when they tap.**

This is the same argument the standing picker rule makes one level up — *a picker is one tap; a
prose list is a writing assignment* — applied to the picker itself. A sentence that says "don't tap
that one" is a reading assignment attached to a button.

### 1.1 Why this is not already solved by what exists

Three mechanisms point at this and none of them closes it:

* **`merge_guard.ask_on_green`** refuses to *ask* about a pull request that is not `OPEN`, and
  `pr_sweep` refuses to ask about one GitHub calls `CONFLICTING`. Both act at **ask time**. A picker
  that went out while its PR was **clean**, and went dirty forty minutes later when a sibling merged,
  is reached by no ask-time check of any kind.
* **`picker_retire`** withdraws a picker whose PR stopped being open or whose head moved. That is the
  right ending, and it is **slow and bounded on purpose**: it costs a `gh pr view` per pull request
  and is capped per pass, so a burst of merges leaves pickers live across several passes. It also
  does not own the conflicted class at all.
* **Prose.** A written warning does not bind — the assistant already knew each time and said so.

**Marking is the fast, cheap, universal layer under the slow authoritative one.** It costs no `gh`
call, has no cap, covers every death class, and lands within one pass.

---

## 2. What `setMessageReaction` actually gives us

`setMessageReaction` is Bot API 7.0. `telegram-capability-map.md` §2.3 filed it **tier 2, genuinely
useful**, and its coverage table (§4) filed it **UNKNOWN-UNKNOWN — surveyed, never weighed.** This is
its first use.

Four properties make it the right instrument, and each one is load-bearing:

1. **No admin right is required in a private chat.** Nothing host-side has to be flipped — unlike
   forum topics, whose @BotFather Mini App toggle no PR can reach.
2. **"The update isn't received for reactions set by bots."** So the assistant's own reaction cannot
   echo back into `telegram_poll`'s inbound path and loop. §8.1 is the residual if that ever stops
   being true, and it is named rather than assumed.
3. **No notification is raised.** Re-marking the whole queue after every merge is **silent and
   free**, which is the property §4 is built on: a marker that can be re-applied every few minutes at
   no cost to the owner's attention does not have to be right the first time.
4. **One reaction per message for a bot.** So a picker carries **one** state — not a rank *and* a
   state. §4 is what that constraint bought.

There is also no time limit on a reaction, unlike `deleteMessage`'s 48 hours: a picker from last
week can still be marked.

---

## 3. Keycap numbers and 🛏 are impossible, and the measurement says so

Telegram restricts reactions to a **fixed server-side allowed set** (`ReactionTypeEmoji`). Verified
twice, independently:

* **Against the live API.** `setMessageReaction` **accepted** 😴 (U+1F634), 👀 (U+1F440), 🔥
  (U+1F525) and 🥱 (U+1F971), and **rejected** the `0️⃣` and `1️⃣` keycaps, 🛏 (U+1F6CF) and 💤
  (U+1F4A4) — every one `Bad Request: REACTION_INVALID`.
* **Against this repo's own tracked copy of the set**, written down in `telegram-inbound-spec.md`
  §3.4b for the inbound half, well before this feature: 😴, 👀 and 🔥 are all in it; 🛏 and the
  keycaps are not.

The two agree. **So keycap priority numbers and 🛏 cannot be sent by a bot, ever** — not an
assistant limitation and not something a later change can unlock. (Telegram Premium's custom-emoji
route is a *sending user's* privilege, not a bot's; §3.6 of the inbound spec covers the direction
that does work, which is inbound.)

**🥱 was accepted and is deliberately unused.** Three states is the vocabulary (§4); a fourth glyph
that means almost the same as 😴 is vocabulary drift. It is recorded here so a future reader knows the
option was weighed.

---

## 4. Three states, one glyph each

| Glyph | Codepoint | Name in code | Means |
|---|---|---|---|
| 🔥 | U+1F525 | `next` | ~~**Tap this next.** At most one picker carries it.~~ **AMENDED — see §6.1: spendable now. No uniqueness.** |
| 👀 | U+1F440 | `queued` | Alive but not green yet — **not yet**. *(Originally also "green but queued behind another"; §6.1 retired that case.)* |
| 😴 | U+1F634 | `asleep` | **Put to bed.** This picker cannot be spent; do not tap it. |

😴 is the 🛏 joke in a glyph the server accepts, and it means exactly what was asked for: *it got put
to bed.*

**This section's "at most one" and the argument below it are the ORIGINAL design, kept rather than
deleted — §6.1 is the amendment and carries the decision against it.** A decision whose alternatives
have been erased is a decision nobody can revisit.

**The original argument for one 🔥 over a numbered list was not that numbers were unavailable.** It
was that a numbered list is **stale the instant something merges**, and *"the one that is 🔥 right
now"* is always current. Merging one pull request routinely dirties the others (one merge dirtying a
sibling three minutes before the next sweep is the common shape), so a merge queue is **inherently
sequential**: after each merge every other candidate has to be re-checked. `1️⃣2️⃣3️⃣` on five
messages would need four edits after every merge just to stop lying. **A marker re-applied after
every pass needs no such maintenance** — because §2's third property makes re-marking free. The
marker is not a stored ranking that has to be kept in sync; it is a **derived view, recomputed from
scratch every pass**. That is why the constraint improved the design: one-reaction-per-message forced
the thing that stays true over the thing that goes stale.

---

## 5. What makes a picker 😴 — mechanical, no judgment

An approval picker is bound to `(repo, pr, head_sha)`. It is **dead** when any of:

1. its `head_sha` is no longer the pull request's head (superseded by a rebase or force-push), **or**
2. the pull request is closed or merged, **or**
3. the pull request cannot merge as it stands, **or**
4. the pull request is BEHIND its base branch (§5.5).

Every one is read off facts the sweep already has, plus — for (3) — one local git call. Nothing here
is a judgment and nothing here is a model call.

### 5.1 The third class is why `git merge-tree` is in the loop

`gh`'s `mergeable` field is **stale-optimistic**: GraphQL and REST have both been observed serving
`MERGEABLE` for **4+ minutes** after a merge had already dirtied the dependent PRs, and that window
is exactly when a tap gets spent on nothing. At ask time there is nothing better available across
several repositories, so the ask path accepts the residual.

**`git merge-tree --write-tree <base> <head>` does not lie**, and the marking pass can afford it where
the ask path cannot. So the mergeability verdict is the union of two sources:

```
cannot merge  =  GitHub says CONFLICTING            (free, on the row this pass already fetched)
              OR git merge-tree reports a conflict  (local, honest, one repo, ~60 ms)
```

**The local arm may only ever ADD deadness, never remove it.** A clean `merge-tree` never clears a
GitHub `CONFLICTING`. That polarity is deliberate: a stale local base could otherwise resurrect a
picker that genuinely cannot merge, and the cost of the two errors is not symmetric — a missed 😴
costs one tap, a wrongly-cleared 😴 costs a tap *and* teaches the owner the marker is unreliable.

### 5.2 Three traps in the local call, each of which would produce a false 😴

* **A missing object exits 1, exactly like a conflict.** `git merge-tree --write-tree origin/develop
  deadbeef…` exits **1** with `not something we can merge` on stderr and **empty stdout**, while a
  real conflict exits **1** with the merged tree's OID on stdout. An unfetched head would therefore
  read as *conflicted*. Two locks: **both refs are proved to resolve to commits first** (`git cat-file
  -e <ref>^{commit}`), and exit 1 is only read as a conflict when stdout is non-empty.
* **The wrong repository.** The sweep may watch several repositories and the process runs inside one
  checkout. The local arm is used **only** for pickers whose repo matches the slug derived from the
  running checkout's own `origin` remote. Everything else falls through to GitHub's field.
* **fsmonitor.** Every git call passes `-c core.fsmonitor=false`; the daemon's checkout can hang
  without it, and `job_worktree.py` already does this everywhere.

**It never fetches.** A head pushed since the last `seneschald-update` fetch (~10 min) is simply not
local, `cat-file` says so, and the picker falls back to GitHub's field. Adding a fetch would put a new
side effect on the daemon's own checkout every pass — the checkout that must stay clean so the
self-updater's fast-forward pull keeps working.

### 5.3 What the local arm does not cover, stated rather than implied

* **Any watched repository other than the daemon's own gets GitHub's field only.** A repo→path map
  is host-side configuration no PR can supply. Named, not silently absent.
* **The ~4-minute stale-optimistic window survives there**, and for any head not yet fetched. The
  residual is bounded by the pass cadence: a picker mis-marked 👀 is corrected on the next pass that
  can see the truth, silently and for free. That self-healing is the property an *ask* does not have,
  which is why the same imperfect field is acceptable here and was not there.

### 5.4 😴 is derived every pass, not latched

It is tempting to make 😴 terminal, on the reasoning that a picker bound to a dead SHA never becomes
valid again. **That reasoning is correct and the latch is still wrong**, for a reason that only shows
up in the third class:

* For classes 1 and 2 the evidence never reverses, so terminality **falls out** and needs no rule.
* For class 3 it can reverse — a conflict is resolved, or GitHub's `CONFLICTING` was computed against
  a base that has since moved. A latched 😴 on a picker that came back to life is a picker the owner
  will never tap, and the merge gate's ask log is keyed on `(repo, pr, head_sha)`, so **no replacement
  picker would ever be sent for that commit** — the silent never-asked failure the PR sweep exists to
  remove, wearing a reaction.
* Class 4 (§5.5) reverses by the identical argument: a plain merge-up clears BEHIND at the same head
  SHA a moved base dirtied.

Deriving fresh costs nothing (§2's third property) and removes the failure mode entirely. So there is
no latch and no stored terminality: **the state is a pure function of the current facts**, recomputed
every pass.

### 5.5 A fourth class: BEHIND, when the base branch requires up-to-date branches

When branch protection enables `required_status_checks.strict`, a branch must be up to date with its
base before GitHub will merge it. Classes 1-3 all leave a trace `mergeable` can answer, while a BEHIND
head reports `mergeable: MERGEABLE` — no conflict at all — with the staleness carried on a
**different** field, `mergeStateStatus`.

**It is kept as its own class rather than folded into class 3, because the two have different fixes.**
A conflict needs a hand resolution; BEHIND needs a plain merge-up, and a picker that names the wrong
repair sends the owner to resolve a conflict that does not exist. The sweep's BEHIND check is the
conflict check's allow-list shape one field over, and its report carries a separate `behind` flag
rather than overloading `conflicted`.

**It composes with everything stated about class 3, unchanged.** It is checked off the row the sweep
already fetched (`mergeStateStatus` joins the listed fields on the same call), it is derived every
pass rather than latched (§5.4), and it can never be cleared by the local `git merge-tree` arm — that
arm answers *would this conflict*, a different question from *is this branch up to date*.

---

## 6. What makes a picker 🔥 — the original tie-break

**AMENDED — see §6.1.** Everything in this section is the ORIGINAL design, kept rather than rewritten
in place. Code does not implement the single-winner tie-break described here; the current behaviour
is §6.1's.

Among the pickers that are **alive** (open, at the pinned head, nothing says it cannot merge) **and
green**, exactly one carried 🔥. The rest carried 👀.

**Green is required for 🔥 and is not enough for 😴.** Required, because the standing rule is *never
merge a PR with red or pending CI*, so pointing the owner at a red PR would be pointing them at a
merge they must not make. Not sufficient for 😴, because pending CI is temporary and red CI is fixed
by a commit, which moves the head, which is class 1 anyway. *(This half survives the amendment.)*

**The original selection rule, in order:**

1. **Stability.** If exactly one alive-and-green picker already holds 🔥, it keeps it. No call, no
   flicker.
2. **Otherwise, the oldest question wins** — smallest `asked_at`, tie-broken by `(repo, pr)` and then
   by question id, so the order is total and deterministic.

**This was a tie-break for determinism, and the spec said so rather than dressing it as a priority
policy.** `asked_at` rather than the PR number because a PR number is not comparable across
repositories — ordering on it would need a *which repo first* preference the owner never stated. 🔥
claimed no leverage: a hand-reasoned merge order ("the tool first, then the free docs-only one, then
whatever unblocks the most") is not something a program can derive, and encoding a leverage heuristic
would be inventing a policy the owner never set.

### 6.1 The amendment — 🔥 means spendable now, no uniqueness

**What surfaced the question.** Two pickers carried 🔥 at once — both green, both clean, **both
genuinely spendable and disjoint.** That breached the original invariant ("exactly one carries 🔥")
even though the *output* was not misleading — both really were things the owner could tap. The choice
was to enforce uniqueness harder in code, or amend the rule. **The mechanism was never reproduced and
is left unverified, deliberately** — §8.2 names one way two 🔥 could transiently coexist, but nothing
tied the instance to it, and inventing a cause would be exactly the kind of unverified claim this
design record argues against. What mattered was whether coexisting 🔥 was a problem at all.

**The owner's decision: the rule is the defect. 🔥 means spendable now, with no uniqueness.**

**The reasoning, which is also the condition under which this would be revisited.** The entire point
of picking an order — numbered or single-🔥 — was to let the owner merge things without hitting a
conflict. When *everything conflicts anyway* (every open PR touches the same hot file), no order can
protect them from a conflict that a merge order was supposed to prevent, so there is nothing left for
single-winner selection to buy over simply marking every genuinely spendable picker 🔥 at once. The
tie-break's determinism was real, but determinism in service of an ordering that can no longer do its
one job is not worth keeping. **The rule was amended rather than enforced harder.**

**What changes, concretely:**

* 🔥 means **"alive, green, and therefore spendable right now"** — a property of ONE picker, computed
  independently of every other picker's state. It is no longer a claim about order.
* **Every** alive-and-green picker carries 🔥. There is no winner, no stability rule keeping a prior
  holder, and no tie-break on `asked_at`; the selection helpers are removed from `picker_mark.py`
  rather than left unreachable, because dead code implementing a rule just called a defect is not a
  neutral thing to leave lying around.
* 👀 keeps exactly one meaning: alive, but **not yet green**. The old second reason — "queued behind
  another green pull request" — no longer exists.
* 😴 is untouched. §5.4's "derived every pass, never latched" argument does not depend on anything §6
  argued.
* §9 q1 is **answered**: no ordering, and not because a leverage heuristic is hard to encode but
  because an ordering has no job left to do while the repository is in that state. §8.2 is no longer
  a residual — coexisting 🔥 is simply the design.

---

## 7. Where it lives, and what it may not touch

```
pr_sweep.sweep()            ask  →  MARK  →  retire
   │                                 │
   │  builds `row_index` off the     └─ picker_mark.sweep(state_dir, row_index, …)
   │  same `gh pr list` it already        │
   │  runs (one new field: baseRefName)   └─ telegram_ask.mark(c, state_dir, qid, emoji)
   │                                             └─ setMessageReaction
   └─ picker_retire.sweep(…)  (unchanged)
```

`picker_mark` has `picker_retire`'s shape exactly: it rides the pass's own list, it decides *which*
picker gets *which* glyph, and `telegram_ask` owns the wire call and the stamp — the same split as
`picker_retire` / `telegram_ask.retire`. In this repository `telegram_ask.mark` is the part that has
shipped; the sweep, the marking pass and the retirement pass arrive with the PR-sweep port.

**Marking runs between asking and retiring.** Asking stays first and is untouched. Marking is second
because it is the cheap, time-sensitive half — no `gh` call, no cap — and because a picker that this
pass will also retire should carry 😴 *before* it is settled, so the settled message ends up with the
marker on it. Retirement stays last, unchanged.

**Hard limits, each asserted by a test:**

* It **never sends a message**. Reactions and one store stamp, nothing else.
* It **never clears a reaction.** Every pending PR picker resolves to one of three glyphs, so
  `reaction: []` is never needed and never sent.
* It **never merges, approves, or records an approval**, and never so much as names
  `merge_guard.record_approval`.
* It **never touches a non-PR question.** Eligibility is `picker_retire`'s own pending-PR-picker
  selector, **imported not re-spelled** — the positive allow-list (`meta.kind == "merge-approval"` +
  an integer `pr` + a readable `repo`) that keeps every other kind of question out of scope by shape.
  A second eligibility rule here would be a second classifier that can drift from the first.
* It **never changes the gate.** A reaction is decoration on a question; the approval path is
  untouched.

### 7.0 The quiet window suppresses the ASKING, not the pass

**This is the one change outside marking that the design makes, and it was forced rather than
chosen.** The sweep used to return early at the merge gate's overnight quiet window, before its first
network call — a **cost optimisation in front of a rule**, since `ask_on_green` re-checks the same
window independently. That was true while asking was the only thing behind it.

**Hanging marking off that early return would have made it false.** A cost optimisation would have
become the thing deciding whether pickers get marked, for the hours of every day that matter most:
owners do tap Approve in the small hours, and a picker that went out clean and went dirty later is
reached by no ask-time check — **a marking pass is the only thing that could reach it.**

**What the window protects is unchanged: nothing new reaches the owner's phone inside it.**

| | Before | After |
|---|---|---|
| Picker sent | never | never |
| Recorded in the ask log | nothing | nothing (so it is deferred, not dropped) |
| `gh pr list` | not called | called — a few hundred read-only calls a night against a 5,000/hour budget |
| Marking | *(did not exist)* | runs |
| Retirement | did not run | runs |

**Retirement rides along, and that is a consequence worth naming.** It is the same category as
marking: an edit to a message the bot already sent raises no notification either, so it cannot wake
the owner — and a picker that is *settled* overnight is strictly better than one that is dead and
still tappable. A special case that suppresses retirement at night and not marking would assert that
one silent operation is louder than the other, which is not true. **If this is wrong it is one `if`
to put back.**

### 7.1 Fail soft, on every path

A marker is a convenience; the sweep's job is asking. So **every failure costs the mark and nothing
else**, and the marking pass never raises:

| Failure | What happens |
|---|---|
| `REACTION_INVALID` or any other Bot API refusal | one line in the report, next picker |
| Network error, timeout, ambiguous send | one line in the report, retried next pass |
| The message was deleted (`message to edit not found` class) | one line, next picker |
| 429 rate limit | `telegram_http` handles it; a failure is one line |
| Credentials missing or unreadable | the whole marking pass reports and returns; asking and retiring are unaffected |
| The question store cannot be read | same |
| `git` missing, hung, or the checkout unreadable | no local answer; GitHub's field alone |
| the marking pass raises anything at all | the sweep records it and **still runs retirement** |

`setMessageReaction` is deliberately **not** added to `telegram_send.IDEMPOTENT_METHODS`. That set is
the *read* allow-list and an unlisted method gets the conservative `UNSAFE` policy, which retries only
what provably preceded delivery. The cost here is at most one skipped mark, recovered on the next
pass — the same self-healing property §5.3 leans on, and a much better trade than widening a set
whose whole value is that it is small.

### 7.2 Idempotence, twice

The pass runs every few minutes and re-derives every state, so it must not re-set a reaction that is
already correct. Two independent skips, deliberately overlapping:

* the marking pass compares the derived state against the record it already read and does not call;
* `telegram_ask.mark` re-reads the store and refuses a no-op regardless of caller.

**The wire call goes first and the stamp second** — the opposite of `resolve`'s durable-first order
and the same order `retire` uses, for the same reason: the thing that must not exist is a record
claiming a reaction the message does not carry, because that record is what stops the next pass
re-applying it. A stamp that fails costs one redundant call next pass and converges.

---

## 8. Residuals, named

### 8.1 If the "no updates for bot reactions" guarantee ever fails

`presence.DEFAULT_REACTION_INTENTS` maps **😴 → `snooze`**. 🔥 and 👀 are unmapped and read as `note`.
So if Telegram ever *did* deliver a bot's own reaction as a `message_reaction` update, the observable
symptom would be a spurious *"the owner reacted 😴 (= snooze) to an earlier message"* line arriving in
the warm session — attributed to the owner, about a message they never touched.

It is bounded: a picker message is not in `state/telegram-message-map.json` (which holds nudges and
chat replies), so it degrades to *"an earlier message"* rather than naming a reminder, and nothing on
that path writes state. But it is written down because the symptom is otherwise unattributable. **The
guarantee is documented and cited, not measured** — measuring it would mean reacting in the owner's
live chat to see what comes back.

😴 was kept in spite of the collision because it is the glyph that carries the "put to bed" meaning.
The alternatives in the allowed set are 🥱 (also mapped to `snooze`) or something meaningless like 🗿.

### 8.2 More than one 🔥 during a repository outage — SUPERSEDED by §6.1

**Under the original design:** the exactly-one property held over the pickers a pass could
**evaluate**. If a repository's list could not be read, its pickers were left as they were —
including a stale 🔥 — while a picker in another repository could legitimately take 🔥, so two 🔥
could coexist until the broken repository read again. Withholding 🔥 from everyone whenever any
repository is unreadable would have been worse. Filed then as *cosmetic, transient, self-healing*.
**After §6.1 this describes a non-event** — coexisting 🔥 is what 🔥 means now. Kept because it
explains why the old design could tolerate two 🔥 transiently even before the amendment.

### 8.3 The stale window, again

Covered at §5.3: for any repository outside the daemon's own checkout, and for any head not yet
fetched locally, a picker can carry 👀 for up to ~4 minutes plus one pass after the merge that killed
it. Bounded, self-healing, and strictly better than never being marked at all.

---

## 9. Open questions — the owner's, and not blocking

**q1 — should 🔥 encode a real merge order? ANSWERED (§6.1): no.** An order only has a job while it
can help the owner avoid a merge conflict; while everything conflicts anyway, there is nothing left
for it to buy. The selection helper is removed rather than left as the place a future ordering
"would go."

**q2 — should a conflicted picker be RETIRED rather than merely marked?** `picker_retire` owns
not-open and moved-head; the conflicted class is marked 😴 and left tappable. Retiring it is stronger
and irreversible, and `mergeable` is a field measured as stale — so a retirement on it could withdraw
a live decision, which is the one thing `picker_retire`'s fail-open rules exist to prevent. Held.

**q3 — is letting the pass run overnight right?** §7.0 argues it, and the argument rests on *a
reaction and an edit both raise no notification, and owners do tap in the small hours.* What is **not**
measured is whether the owner wants the assistant doing anything at all in that window, which is a
different question from whether it disturbs them.

**q4 — should marking cover every repository with pending pickers?** Pickers for repositories outside
the sweep's watched set live in the same store, and the resident pass never *asks* about them. The
CLI covers them; widening the watched set for asking is the sweep's decision (§10.5 item 1).

---

## 10. The gaps the marking pass does not cover

§9 q4 states the unwatched-repository gap as a design question. Looking at what the question store
actually held corrected its shape: the repository scope is real, it is not the whole cause, and the
store holds a second failure that widening the watched set would not touch.

### 10.1 A phantom question outliving its PR

The observed instance: a picker for a PR in an **unwatched** repository stayed unsettled for days
after the PR merged — the **oldest** unsettled record in the store — and was surfaced to the owner as
an open decision, who then decided it again, on a pull request that had already merged.

### 10.2 Why it was never retired

The resident pass never makes an unwatched repository's picker a candidate **for asking** — still
true, and still the decision (§10.5 item 1). Originally that also meant it was never *retired*:
`picker_retire` read only the repositories the pass had listed.

**Retire-only widening.** The same failure one repository over — a picker left standing after its PR
merged, which the owner tapped *Not now* on just to make it stop looking undecided — settled it: the
resident pass lists the watched repositories **∪ every repository a pending retirable picker names**
(the CLI's own enumeration, shared rather than forked, and capped per pass with the remainder named
in the report as held over, never dropped). The extra rows reach **only** the marking pass (a
reaction on a message already on the owner's phone; its local `git merge-tree` arm stays scoped to
the daemon's own checkout, so an unwatched repository can never trigger it) and the retirement pass
(an edit to that same message). **They never enter the ask candidates and never reach the red-CI
arm**, so no picker and no notice can originate from an unwatched repository — the watched set is
unchanged, and item 1 below is not reversed. Every decline case in `picker_retire` applies unchanged
to the extras. A test pins both halves: the merged-PR picker retires, and a green, open, un-asked PR
in the same unwatched repository produces no ask, no notice and no ledger row.

**Two candidate causes are NOT the reason, checked rather than assumed.** A null `message_id` does
not block retirement — `telegram_ask.retire` answers a missing message id by stamping `retired_edit:
"no-message"` and retiring anyway. And the record was a well-formed `merge-approval` with an integer
`pr` and a readable `repo`, so the kind filter does not reach it either.

### 10.3 The sharper finding: the decision was never actually missing

The phantom's `message_id` was `null` because **the send did not land** — that picker was never on
the owner's phone. A second picker for the identical question at the identical head went out minutes
later, landed, and **the owner answered it before the merge.** The gate worked exactly as designed.

What survived was the **failed send**. Both records carried the same `(repo, pr, head_sha)` — the
exact tuple the merge gate uses as an ask identity — and **the answer on one did not settle the
other.** Nothing deduped at the record level, so a failed send left a phantom question that outlived
both its pull request and its own answer. **§10.6.1 closes it.**

### 10.4 The class q4 does not cover: an answer given in conversation

`answered_at` is written in exactly one place — `telegram_ask.resolve`, the callback path. **A tap is
the only thing that settles a record.** A question the owner answers in chat stays pending forever
unless something else retires it — observed: a question answered in conversation, whose decision
was implemented within minutes, still sat in the pending list days later and was asked again. It was
not a PR picker, so `picker_retire`'s positive allow-list puts it out of scope **by shape**: no
existing mechanism can settle it, and none of them is wrong to refuse, because retiring a question on
an inference about a conversation is exactly what the fail-open rule exists to prevent — *a
wrongly-retired live question is far worse than a stale one.*

**What closes it is a verb rather than a mechanism** (§10.6.2): nothing infers anything, the
assistant says so and quotes the owner, and the record carries the quote. It is deliberately the one
path here that no allow-list narrows.

### 10.5 What this licenses, and what was decided

Three separable pieces of work fall out, each a code change with its own tests. They went to the
owner as one multi-select picker, and **two of the three were taken**:

1. **Widening the watched set for asking.** It closes §10.1 and neither of the others. **DECLINED.**
   The argument that carried it: **widening the net before fixing the drain makes the pile grow
   faster** — the two fixes taken *drain* the store, while adding more repositories to the ask set
   would have *filled* it faster. So the watched set is untouched, `picker_retire`'s decline case
   still names the gap honestly, its CLI still covers every repository with a pending picker, and
   **q4 remains open.** A future change that widens the ask set is reversing a decision, not filling
   a hole. (The retire-only widening in §10.2 is narrower than this item and does not touch it.)
2. **Settling a record when a different record for the same `(repo, pr, head_sha)` is answered.** The
   identity already exists in `merge_guard`; nothing joined the two records on it. **SELECTED —
   §10.6.1.**
3. **A path by which a question answered out-of-band can be settled at all.** By far the most
   dangerous of the three: a mechanism that infers *the owner answered this in chat* can withdraw a
   live decision they were never shown. **A decision before it is a feature** — and the decision was
   the **explicit** form. **SELECTED, as a verb the assistant invokes, never a sweep — §10.6.2.**

The per-instance cost of these gaps is small and it lands entirely on the owner, as re-decisions.

### 10.6 What items 2 and 3 became

Both are **store-only**. Neither sends a message, edits one, sets a reaction, or needs a credential:
they change what the store says a question's state is, and nothing about what is on the owner's
phone. That is what makes both reversible (§10.6.3), and it is why `settle-in-chat` runs from any
checkout rather than only the daemon's.

**One vocabulary, not a third.** Both write the keys `telegram_ask.retire` already writes —
`retired_at`, `retired_reason`, `retired_edit` — plus one new key, **`retired_by`**, which says *how*.
`retired_edit` is `not-attempted`, distinct from `no-message`: *the message was never touched* and
*there was no message to touch* are different facts, and the second is §10.1's whole instance.
**`answered_at` stays null and `selected` stays `[]`, forever, on both paths.** Those two fields are
what the merge gate reads as the floor under every approval, so a settled record can never be
mistaken for a tap — not by the gate, not by a reader of `telegram_ask list`, and not by any future
count of how many decisions the owner actually made. `list` carries `retired_by` on every row that
has one, because a field nobody can read is not a record.

`retired_by` is **opaque to `telegram_ask` exactly as `meta` is** — it carries it and never reads it.
The module that decided a record is settled owns the word for why: `twin-answer` is spelled in
`merge_guard`, `in-chat` in `telegram_ask`.

#### 10.6.1 The twin settle — `merge_guard.ask_identity` + `twin_settlements`

**When one picker is answered, every other unsettled record carrying the same `(repo, pr, head_sha)`
is settled with it**, in the same `resolve` call, in one store write. It fires on any **answer** —
Approve *and* "Not now" — because being asked the same question twice costs the same either way.
`telegram_ask.resolve` calls it behind an optional `merge_guard` import: with no merge gate installed
there is no twin dedupe, and a duplicate stays pending until it expires or is settled by hand — the
safe failure.

**The identity is exposed, not invented.** `merge_guard.ask_identity(meta)` returns the tuple the ask
log is keyed on and an approval is bound to, read off a question record's own `meta`. **A missing
part is `None`, not a tolerance** — deliberately stricter than the ask-log lookup, whose legacy rows
match on an absent repository. The polarities are opposite for a reason: there a wrong answer costs
at worst a duplicate picker, here it would settle a question the owner is still owed.

**The approval-corroboration check is left alone, on purpose.** It answers a different question —
*does the store corroborate this approval?* — must name which field disagreed in a sentence a denied
merge prints, and keeps a legacy tolerance it must keep. Rewriting it onto the stricter tuple would
newly deny approvals that verify today — a behaviour change dressed as a refactor. A test binds the
two in the direction that matters instead: **where `ask_identity` says two records are the same
question, the floor under every approval agrees.**

**There is no inference in it, and that is the design.** A twin is literally the same question at the
same commit:

* a **different head SHA is a different question** and is left alone — the re-ask-at-a-new-head
  binding, which this may never defeat;
* a different repository at the same PR number is left alone — and the repo key case-folds, so a slug
  differing only in capitalisation is one repository, which is how GitHub and NTFS both read it;
* a record of any other `meta.kind` is out of scope **by shape**, including §10.4's kind;
* **only an ANSWER propagates.** A retirement never cascades and neither does a settle, so nothing
  here can chain.

`twin_settlements` is **pure** — a store dict in, a list of `mark_settled` arguments out — and the
write is `telegram_ask`'s, in one save, so the case is atomic rather than N chances to crash half-way.
The whole block is wrapped: the owner's answer is on disk before it runs, and **a stale twin is the
safe failure while a lost answer is not.** A tap that lands on a settled twin afterwards is safe
without any message edit at all — `resolve`'s retired branch returns no `meta` and no `answered`, so
it is *structurally* incapable of minting an approval, records nothing, and says so.

#### 10.6.2 `settle-in-chat` — a verb, not a sweep

`telegram_ask.py settle-in-chat --question-id <id> --quote "<what the owner actually said>"`.

**It refuses without the quote, and the refusal lives in the function rather than the argument
parser.** `ask`'s citation gate makes the same move for the same reason: a check in `_cmd_*` is a
check an in-process caller routes around without noticing. The quote is the whole safety property —
**a settle with no provenance is the inference version wearing a coat** — so the record has to be
able to show *what the owner actually said* to whoever reads it back, including the reader who thinks
the assistant misheard. `--quote` is deliberately not argparse `required=True`: the missing-quote path
is the one a caller most needs a reason for, and argparse's answer to it is a usage dump.

The record names **who settled it, and it is the assistant — never the owner.** The owner gave the
answer; the assistant made the claim that they gave it, and a record that cannot tell those two
apart is the thing this is trying not to be. The **full** quote is kept verbatim on
`retired_by.quote`; `retired_reason`, which is what a later tap shows in a popup, carries the first
120 characters.

**Nothing may call it automatically, and a test enforces that** — built in the shape of
`test_pending_checks.py`'s no-importer guard, which scans for a **string** rather than an import,
because the danger is a `subprocess.run([… "settle-in-chat" …])` that no import graph would show. It
reads executable files only (`.py`, `.ps1`, `.sh`, `.yml`, `.yaml`) across the tree: a `.md` naming
the verb is the assistant being told the verb exists, which is the feature, and **a prompt is not a
program.**

Unlike the twin settle, it narrows on nothing — §10.4's instance would be out of scope under any
allow-list this file's other mechanisms use.

#### 10.6.3 Reversible, and BOTH ways rather than either

An un-settle path, or a marker `list` still surfaces? **Both, because they answer different
failures.** `telegram_ask.py unsettle --question-id <id>` hands the question back — the stamps come
off, the picker taps again, and the undone settle is kept on the record as history, because *"this
was settled and then un-settled"* is exactly what someone re-reading a re-decision wants. And `list`
shows a settled record with its `retired_by`, so a wrong settle is **findable** rather than merely
undoable; an undo nobody can discover they need is not a recovery path.

The assistant claiming it heard an answer is a claim, and **the recovery from a wrong claim has to be
cheaper than the claim** — otherwise the safe move is to never settle anything and the verb is
decoration. That is also why neither path edits the message: the keyboard is still on the owner's
phone, so an un-settle genuinely hands the question back.

`unsettle` **refuses a `picker_retire` retirement**, which carries a `retired_edit` of `edited` /
`already-settled` / `no-message` and no `retired_by`. Those messages are edited down to one settled
line with no keyboard, so undoing the record would leave a question that reads live in `list` with
nothing on the phone to tap — a worse state than either end. Ask again instead.

#### 10.6.4 What neither of them does

* **Neither widens the watched set for asking**, per item 1. The store-level twin settle reaches
  unwatched repositories anyway — it runs on the owner's tap, not on the sweep.
* **Neither edits, deletes or reacts to a message.** `picker_retire` owns the message and the marking
  pass owns the reaction. A settled record stays a tappable picker whose tap records nothing — see
  §10.6.5.
* **Neither writes an approval, and neither can.** `merge_guard.record_approval` has exactly one
  caller, on a real `callback_query`.
* **Neither infers that a question was answered.** Item 2 propagates an answer that exists; item 3
  propagates a claim a human made in the moment and quoted. The automatic version of item 3 — a sweep
  that reads the conversation and concludes the owner answered — is unbuilt and still the most
  dangerous of the three.

#### 10.6.5 The residual: a settled picker keeps its keyboard

**The retirement and marking passes both skip any record carrying `retired_at`** — correctly, since a
settled record has nothing left to decide. So a record settled here is never edited down to a
settled line and never marked 😴: **its message stays in the thread with a live-looking keyboard.**

**In a watched repository this is a small step backwards, and saying so is the point.** Before, the
owner answered picker A, the merge closed the pull request, and the next sweep retired picker B by
editing its message. Now B is settled on the tap, so the sweep skips it and the message is left alone.

It was not fixed here, for three reasons, in the order they weighed:

1. **The tap is the whole point of the path and it may not get slower.** `resolve` is durable-first —
   decide, persist, *then* talk to Telegram — and the client spins the button until
   `answerCallbackQuery` lands. Editing N twins before that is N network calls in front of the
   owner's button; editing them after it is a call nothing retries, because no sweep looks at settled
   records.
2. **The outcome is already handled and is honest.** A tap on the surviving keyboard lands on
   `resolve`'s retired branch, which returns no `meta` and no `answered` — *structurally* incapable of
   minting an approval — records nothing, and says so in the popup.
3. **`retire`'s invariant runs the other way and this path cannot honour it.** There the edit goes
   FIRST and the stamp second, so a record is never marked retired while a live keyboard is on the
   phone. A settle that happens *because the owner just answered* cannot wait on a network call for
   its stamp without putting the answer behind one.

**The fix, if it is ever wanted, is `picker_retire`'s** — letting the sweep tidy the message of a
record that is already settled, without re-stamping it. That is a change to a module governed by
several fail-open decline rules, and it belongs in a change about that module.

## Summary

A merge picker's **state goes on the picker as one reaction**, because a prose warning arrives in the
conversation while the tap happens on the button. Telegram's allowed reaction set has no keycaps and
no 🛏, and a bot gets one reaction per message, so the vocabulary is three glyphs — **🔥 spendable
now · 👀 alive but not green · 😴 put to bed** — re-derived every pass (a reaction raises no
notification, so re-marking is free), **never latched** (a conflict can clear; a latched 😴 would be
a picker nobody ever re-asks). `git merge-tree` backs up GitHub's stale-optimistic `mergeable` field
and may only ever *add* deadness. Two store-only follow-ons close the gaps the marking pass cannot
reach: an answer on one picker **settles its twins** (same `(repo, pr, head_sha)`), and
**`settle-in-chat`** records an answer given in conversation — a verb the assistant invokes with a
verbatim quote, never a sweep — both reversible via `unsettle` and visible in `list`.
