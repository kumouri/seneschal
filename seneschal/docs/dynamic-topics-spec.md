# Dynamic topics — minting a Telegram topic mid-conversation, no PR, no reload

**Status:** `PARTIAL(§0-§2's core capability BUILT — telegram_topics.py add/retire over the existing runtime state file, a simpler mechanism than §1's design; §3's declaration-driven routing waits on message-routing-spec.md phase 2 and the presence.py wiring; §4's promotion, §5's policy for a promoted purpose, §6's sprawl controls and §7's gate recommendation remain open)`
· **Owner:** the assistant. **Scope:** `../scripts/telegram_topics.py`'s purpose→name table and its
two readers (`create_topic`/`thread_id`, and `../scripts/channel_declare.py`'s
`resolve_purpose`/`grounding_instruction`). Does not touch the Bot API surface, `presence.py`'s
delivery mechanics, or `message-routing-spec.md`'s own phase gate.

**STATUS NOTE — what actually shipped is narrower than §1's design, on purpose.** The requirement is
not "a topic should be data instead of code"; it is **"a topic needs no PR at all."** A tracked-JSON
routing table still costs a PR per topic, which is what this spec exists to fix. What shipped closes
exactly that gap, reusing §1's base-∪-overlay shape but **on the EXISTING `state/telegram-topics.json`**
— the same file `create_topic` already writes `message_thread_id` into — rather than the NEW
`state/telegram-topics-overlay.json` §1 designs below. `telegram_topics.py add <purpose> "<Title>"`
mints AND creates the Telegram topic in one command (no separate `mint` verb deferring creation,
unlike §2), and `retire` covers §5's option 2 for a runtime-added purpose only. **Everything from §4
on is untouched**: no promotion mechanism exists, so an overlay-only purpose survives only as long as
`state/` does; §5's policy for a purpose that has ALREADY been promoted to the reviewed table, §6's
sprawl controls, and §7's act-low/ask-high recommendation are all still open. A reader should treat
§0-§3 below as *design reasoning* for a decision that landed differently, not as a description of
what runs today — `telegram_topics.py`'s own module docstring is the current source of truth for the
shipped mechanism.

**Why this exists.** The routing was always meant to support this: the owner pops up a new idea or
plan at literally any point, and the assistant creates a new topic for it and routes there — without
a PR and without a reload.

---

## 0. What is already true — verified, so nobody rebuilds it

**"Without a reload" is already solved.** `_load_topic_names` re-reads the routing table
(the gitignored per-install `telegram-topics.json` in `../references/`, falling back to the shipped
`../references/telegram-topics.example.json`) fresh on every `thread_id`/`create_topic` call — it is
deliberately not cached beyond the module-level `TOPIC_NAMES` snapshot taken once at import, which
only `--help` text and test assertions read. A row added to the table resolves with no reimport and
no daemon restart. `test_telegram_topics.py` exercises exactly this. **Nothing about "reload" is this
spec's problem.**

**Topic creation already exists and is exercised.** `create_topic` calls Bot API 9.4's
`createForumTopic`, and `thread_id` already calls it whenever a *known* purpose has no stored thread
yet — `telegram_send.py --topic <purpose>` on a purpose the table knows but has never used creates
its topic on the first send. **Creating a topic for a name the table already knows is not the gap.**

**Runtime thread ids already live in state, separately from the name table, precisely because there
is no `getForumTopics` in the Bot API** (`telegram_topics.py`'s module docstring, constraint 2).
`state/telegram-topics.json` is gitignored and holds `{purpose: {message_thread_id, name,
created_at}}` — the only record of what a bot-created topic's id actually is. **So state-as-truth for
this feature already has a working precedent**; this spec extends the *name* side of the table the
same way, not the *id* side, which is untouched.

**The actual gap, stated precisely.** A purpose must already be a **key in the reviewed table**
before `create_topic`/`thread_id` will do anything with it (`create_topic` looks the purpose up in
`_load_topic_names` and returns `None` for an unknown one). The reviewed table is a file the daemon
must not write: a daemon that edits a tracked file in its own checkout makes `git pull --ff-only`
fail the moment an unrelated merge touches the same path, and the daemon then silently stops
updating itself. So without this spec the owner cannot invent a topic mid-conversation, which is the
entire capability.

---

## 1. Design — an overlay, not a replacement

The reviewed table stays the base, exactly as it is today, changed only by a deliberate edit (a
reviewed PR, or the owner editing their own config). A new, gitignored
`state/telegram-topics-overlay.json` holds purposes minted at runtime:

```json
{
  "schema": "seneschal.telegram.topics-overlay/1",
  "topics": {
    "<purpose>": {"name": "<Display Title>", "minted_at": "<UTC stamp>", "minted_by": "<who>"}
  }
}
```

Same directory convention as `state/telegram-topics.json` (`DEFAULT_STATE_DIR`), same atomic-write
discipline every other `state/` writer here uses (`stateio` / tmp + `os.replace`, never a truncating
write).

`_load_topic_names` gains a `state_dir` parameter (defaulted to `DEFAULT_STATE_DIR`, so the two
existing no-argument callers in `channel_declare.py` — `resolve_purpose` and `grounding_instruction`
— pick up overlay support with no call-site edit) and returns **base ∪ overlay, base winning on
collision**:

```python
def _load_topic_names(references_dir=REFERENCES_DIR, state_dir=DEFAULT_STATE_DIR) -> dict:
    base = _load_base_topic_names(references_dir)      # today's function, unchanged
    overlay = _load_overlay_topic_names(state_dir)      # new, same tolerant-on-every-path contract
    merged = {**overlay, **base}                        # base wins on a shared key
    return {k: v for k, v in merged.items() if k != _RESERVED_MAIN_CHAT}
```

**Base wins on collision, not overlay**, for one reason: once a promoted purpose is reviewed (§4),
the reviewed row is the one that went through a human's eyes. If a stale overlay entry ever survives
alongside a since-renamed or since-retired reviewed row (§5 — promotion is additive and never deletes
the overlay entry it promoted), the reviewed answer must not lose to the unreviewed one that produced
it.

**`_RESERVED_MAIN_CHAT` is filtered on the merged dict, not on each source separately** — the posture
the existing loader already takes on the base file alone: a `main` row would let a future edit to
DATA alone quietly undo an invariant that is otherwise enforced only by review. That invariant is
structural; it must not depend on which of two data files someone edited.

`_load_overlay_topic_names` shares `load_state`'s tolerance ladder exactly: a missing file,
unparseable JSON, a non-dict body, a `topics` value that isn't a dict, or a non-string key/value are
each dropped rather than raising — a broken overlay degrades to "no runtime-minted purposes known,"
never a crash, matching every other rung of this module's fail-open ladder.

`create_topic`/`thread_id` gain the same `state_dir` threading (they already take `state_dir` for the
runtime-id store, so this is one additional argument to one already-present call, not new plumbing).
Everything downstream — `createForumTopic`, the fail-open ladder, the runtime-id write to
`state/telegram-topics.json` — is byte-identical whether the resolved name came from the base table
or the overlay, because both feed the same `name` variable.

---

## 2. Minting is a distinct act from routing — never folded into `--topic`

**The unknown-purpose branch must keep meaning exactly what it means today.** `thread_id`'s "purpose
not in the table" case is the safety net for a typo — `resolve_purpose`'s fuzzy-match ceiling and
fallback to `main` (`message-routing-spec.md`): *if you choose a topic you think exists and it
doesn't, go to main.* **If naming an unrecognized purpose in `--topic` or `[[channel:PURPOSE]]`
silently minted a new topic, every misspelling would become a forum topic**, which is exactly the
sprawl failure (§6) and a direct reversal of that routing decision.

So minting needs its own call, separate from resolving one:

```
python telegram_topics.py mint --purpose <slug> --name "<Display Title>"
```

- Validates `<slug>` against the same charset `channel_declare.CHANNEL_DECLARATION_RE` accepts
  (`[A-Za-z0-9-]+`, lower-cased) — a purpose that a declaration line could never actually name is not
  worth minting.
- Refuses if `<slug>` already resolves in base ∪ overlay (mint is create-only; it is not the
  mechanism for a rename — see §5) or if `<slug>` normalizes to `main`.
- Writes one entry to the overlay file, atomically. **No Bot API call happens here at all** — actual
  `createForumTopic` still happens lazily, on the first real send that names this purpose, through
  the same `create_topic`/`thread_id` fail-open ladder every existing row goes through. Mint is pure
  metadata: cheap, and — because nothing external has happened yet — trivially reversible by
  deleting the overlay row, right up until the first real send creates the Telegram-side topic.

**This directly answers the requirement**: naming a purpose (`--topic`/`[[channel:…]]`) still only
*routes*, unchanged; a caller has to reach for `mint` *in addition*, as a deliberate second act,
before an unrecognized name can ever become real. A model cannot back into creating a topic by
misspelling one. *(As built, `add` both mints and creates in one step — the deliberate-second-act
property holds; only the lazy creation was dropped.)*

---

## 3. The near-miss: does the channel-declaration path skip the create step?

`channel_declare.resolve_purpose` resolves a reply's `[[channel:PURPOSE]]` declaration, but in
`message-routing-spec.md`'s phase 1 the resolved purpose is **only logged** — the reply still goes
wherever the inbound thread says. So a reply that declares a purpose that has a table row but no
stored thread yet never reaches `thread_id`, and `create_topic` never runs off this path.

**This is not a bug to fix here — it is `message-routing-spec.md`'s own, already-designed phase 2.**
That phase moves the reply's delivery from the inbound thread to the declared, resolved purpose, and
it does so by calling `send_telegram(..., topic=<purpose>)` — i.e. through `telegram_send.py`'s
`--topic` handling, **which already calls `telegram_topics.thread_id()`, which already creates the
topic if it has a name but no thread.** So phase 2 closes this gap for free, and it composes with
§1's overlay with no new mechanism at the `presence.py` call site. In this repository the
`presence.py` side of that wiring lands with the daemon-wiring port; until it does, declaration-driven
routing is logged, not delivered.

**What this spec adds on top of that plan:** until phase 2 is live, **a purpose that exists only in
the overlay is invisible to the channel-declaration route in exactly the same way a
table-known-but-threadless purpose is** — declaring it gets logged, not routed. The capability — "pop
up a new idea and route there" — therefore needs its live routing to go through the paths that
already resolve purposes correctly: `telegram_ask.py --topic <purpose>` and `telegram_send.py
--topic <purpose>`. **Mid-turn minting composes with the *existing* routing doors, not with a
not-yet-live phase of the declaration mechanism.** That composition is free once phase 2 ships, but
it should not be assumed working before then.

---

## 4. Promotion back into the reviewed table — additive only

On an install that tracks its routing table in git, a runtime-minted purpose that turns out to be
durable should eventually be promoted into the reviewed table. The tempting shortcut — mirror the
overlay into a development checkout and overwrite the tracked file wholesale, "because nobody else
touched it" — fails on two independent axes.

### 4a. "The file was never touched" is not durably true, and a whole-file overwrite is a two-writer race

The premise — that the reviewed table can be safely overwritten because nothing else could have
changed it — fails the moment any legitimate, human-authored change touches the same file: a
rename, a retirement (§5), or a typo fix in an existing row. "The file was never touched, so
overwrite" checks the wrong thing — whether *the daemon's own copy* was edited, not whether the
*reviewed file* moved since the daemon last looked. That is the classic lost-update shape: **two
independent writers of one file, no lock, and the outcome decided by whichever save lands last.** A
whole-file overwrite here is exactly that, with a human's reviewed change as one writer and the
daemon's push as the other.

**The fix: never overwrite, only append what's missing.** A promotion **stages only the overlay rows
whose purpose key is absent from the reviewed table** — never editing, reordering, or removing an
existing row (the `{**reviewed, **overlay}` merge where the reviewed side always supplies whatever key
it has). This makes the promotion **order-independent with respect to the reviewed table's own
history**: it does not matter whether a human's rename/retirement landed before or after the overlay
accumulated new rows, because the promotion never touches a key the reviewed file already has. A
real merge conflict is still possible if two promotions race each other, but that is an ordinary,
visible conflict on a small structured file — not a silent lost update.

### 4b. Never write into a checkout that belongs to a human

An automated write into the owner's own development checkout — even an additive one — lands in a
tree reserved for their in-flight, uncommitted work. The unit of collision is the **checkout**, not
the branch: concurrent jobs on different branches still collide when they share one working tree.
Being additive narrows the *data* risk (§4a) but does nothing about the *checkout* risk, which is a
different axis entirely.

**The alternative uses the pattern delegated work already uses:** the daemon (or a Dream step) opens
its **own transient worktree cut from the integration branch** (`git -c core.fsmonitor=false worktree
add --detach <path> origin/develop`, `checkout -b`, then `git worktree remove` on completion — the
shape `job_worktree.py` already implements), stages **only** the routing table with the additive
rows from §4a, commits with a Conventional Commit message, and opens a PR — never touching the
owner's checkout, never touching the daemon's own live checkout, and never needing a lock against
either.

**This PR does not skip review, and that is a feature, not a gap.** A promotion PR goes through the
ordinary merge-approval path like any other `references/**` change, and the owner sees exactly which
minted purposes are being made permanent before they are. That review point is worth keeping
deliberately — see §6.

**On a default install the table is gitignored owner config** (`telegram-topics.json` beside the
tracked `.example.json`), so there is no PR to open: promotion degenerates to an additive write into
that config file, under the same append-only rule as §4a. Whether the daemon may make that write
unasked is §7's question.

---

## 5. Deletions and renames — genuinely open, not decided here

**An overlay can only add.** `telegram_topics.py` already has a standing posture that there is **no
delete and no archive path** for a topic — `closeForumTopic`/`reopenForumTopic` don't work in private
chats, and `deleteForumTopic` is destructive and would be ask-high (module docstring, "What it
deliberately does NOT do"). This spec does not touch that. So renaming or retiring a **minted**
purpose raises a question that never had to be answered while nothing could mint one outside a
reviewed change.

Three shapes, costed, none chosen:

1. **No mechanism at all — forward-only, matching the existing "no delete, no archive" posture.** An
   overlay entry, once minted, exists until promoted (§4) or until someone hand-edits the gitignored
   overlay file. Cheapest, consistent with the existing invariant, but a wrongly-named or abandoned
   minted purpose sits in the overlay indefinitely — contributing nothing to §6's sprawl directly (a
   mint with no messages costs nothing but the row) but also never cleaned up.
2. **A `retired` flag on the overlay entry.** A retired purpose no longer resolves (falls through to
   the unknown-purpose branch → `main`, exactly as if it had never existed), but its row — and its
   Telegram-side topic, if one was ever created — is never deleted, matching the no-deletion
   invariant. Cheap (`resolve_purpose`/`_load_topic_names` skip a row with `retired: true`),
   reversible (un-retiring flips the flag back), but only ever covers **overlay** entries — a
   *promoted* row cannot be retired this way without an ordinary deliberate edit, because the daemon
   never writes the reviewed table directly. *(This is the shape that shipped, as `retire`.)*
3. **Rename support, split by promotion state.** An unpromoted overlay entry's `name` can be freely
   edited by the daemon (nothing external has happened yet — or if a topic exists, its title is not
   retitled; see below). A **promoted** purpose's row can only be renamed by an ordinary deliberate
   edit, since the daemon never edits the reviewed table. This is orthogonal to renaming the
   **Telegram-side topic title** itself — `telegram_topics.py`'s docstring already excludes that
   (`editForumTopic` is not used: the assistant creates the topic, so it is named explicitly at
   creation and never renamed). This option is only ever about the purpose→display-name mapping.

**This needs the owner's decision, not a default.** The three shapes have real, different costs — (1)
costs nothing to build and leaves debris; (2)/(3) cost a small amount of code and give the owner a
way to walk something back.

---

## 6. Sprawl — the failure mode, named plainly, and options for the owner

**The failure mode, stated without softening.** A topic per new idea "at literally any point" leads,
unmanaged, to a Telegram forum with a growing number of single-message topics that nobody revisits —
clutter in exactly the surface topics exist to keep uncluttered (the founding complaint: approval
pickers buried in a day's conversation are hell to scroll back through). A topic that exists only
because a passing idea got minted and never returned to is strictly worse than no topic for that
idea, because now there are **two** places it could be (main and the dead topic) instead of one.

**Options, none imposed:**

- **A naming/prefix convention** distinguishing a daemon-minted, not-yet-promoted purpose from an
  established one (e.g. "provisional" in the display title until promotion) — cheap, purely cosmetic,
  a visual signal for which topics are still new without changing any mechanism.
- **The retire path from §5, option 2**, used as the cleanup mechanism: a topic that never grew
  beyond its first message becomes a candidate for retirement, by the owner's say-so or by a review
  prompt (below).
- **A periodic review prompt**, mirroring the shape `../references/proposed-learnings.md` already
  uses — surface, never act. E.g. a nightly or weekly Dream line naming "topics minted in the last N
  days with fewer than K subsequent messages" as a candidate list to retire or leave alone. Surface-
  only by construction and costs nothing beyond a read over the overlay + a message-count check.
- **A same-turn narration requirement** on mint — the discipline act-low actions already carry ("say
  what landed in the same turn," `../references/autonomy-policy.md`) — so a minted topic is never a
  silent surprise the owner discovers later scrolling the topic list. The cheapest option; it
  composes with any of the above; it does not fix sprawl, but it removes the "abandoned topic nobody
  knew existed" version of it.

**This is explicitly the owner's decision.** None of the above is a limit this spec imposes; they are
costed so the owner can pick, combine, or decline all of them.

---

## 7. The gate — act-low or ask-high, and it depends on who decides

**Creating a Telegram topic whose name is already in the table has never been gated at all** —
`create_topic`/`thread_id`'s fail-open ladder runs unconditionally, because *which purposes exist* was
fixed by a deliberate edit before the daemon ever ran. **The gap this spec closes is entirely about
who may cause a *new* purpose to exist without such an edit — the only part new enough to need a gate
decision at all.**

Applying `../references/autonomy-policy.md`'s five graduation gates to *minting* finds a real
tension, and the honest answer is that **it splits on who is deciding, not on the action itself**:

- **When the owner is the one deciding** — they say "put this in its own topic" or name the new topic
  themselves — minting is executing an explicit instruction: the owner supplied the judgment, the
  assistant does the mechanical part. **Act-low.** This is also the literal shape of the request —
  the owner pops up an idea and the assistant creates the topic — the trigger is the owner's
  utterance, not the assistant noticing something unprompted.
- **When the assistant is the one deciding** — inferring, unprompted, from a turn's *content* that it
  deserves its own topic — this is the graduation gate that requires an action be bounded and
  well-defined rather than a judgment call, and "is this topic-worthy" is a judgment call.
  **`topic-mirroring-spec.md`'s three-signal framework already names this split** — the owner's
  explicit say-so is *reliable by construction: it is the owner's decision, not an inference about
  it*, while content classification is *the only signal that is a guess* and should never run
  unattended until it has its own measurement, the same discipline `router.py`'s classifier was held
  to. Minting on a content guess should be held to that bar — shadow/measured before it can act
  unattended, not ask-high forever by default, but **not act-low today**, because no such measurement
  exists yet.

**The closest existing precedent for this exact split is the Archon roster**, and it draws the same
line: *deploy/admit/delegate* to an already-minted Archon is act-low on the owner's standing
authorization, but **mint/revise/retire — deciding whether a *new* roster member should exist at
all — stays ask-high** (`../references/autonomy-policy.md`). A minted Telegram topic is a roster
change for the owner's Telegram workspace in the same sense a minted Archon is a roster change for
their staff. **Recommendation, following that precedent: mint is act-low when the owner's own words
trigger it, and ask-high — or gated behind the measured-shadow discipline the topic-hint classifier
already owes `topic-mirroring-spec.md` — when the assistant would be minting on its own read of
content alone.** Offered as a recommendation for the owner to confirm, not as a decision.

---

## 8. What this spec does not do

1. The shipped build (`add`/`retire` over the existing state file) is the only part built; the
   separate overlay file, the lazy `mint` verb and the promotion mechanism are spec only.
2. Does not move `message-routing-spec.md`'s own phase 1→2 gate (§3), and nothing here is contingent
   on it moving.
3. Does not decide §5 or §7's recommendation — both are named as needing the owner's word.
4. Does not touch `topic-mirroring-spec.md`'s own open forks (opt-in-per-topic, whose messages get
   copied, backfilling) — a different feature, sharing only the same underlying table.
