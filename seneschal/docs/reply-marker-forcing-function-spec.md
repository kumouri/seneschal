# Reply-marker line — a second required opening line on the Telegram reply path

**Status:** `PARTIAL(channel_declare.py's reply-marker check, extractor, combined retry prompt and /2
log fields BUILT; the presence.py drainer wiring and §9's empty-reply guard BUILT)` —
`has_reply_marker`, `extract_reply_marker_line`, `retry_prompt`, `REPLY_MARKER_RETRY_PROMPT_TEMPLATE`,
`CHANNEL_AND_MARKER_RETRY_PROMPT_TEMPLATE` and the `reply_marker_*` fields on `record_outcome` all ship in
`seneschal/scripts/channel_declare.py` (tests: `seneschal/scripts/test_channel_declare.py`). The
drainer runs them, with the empty-reply guard in front (`presence.drainer_task`).

**Scope.** A second required opening line on `presence.py`'s Telegram chat-turn reply path,
generalizing `message-routing-spec.md`'s `[[channel:PURPOSE]]` forcing function:
`*(answering <what you're replying to>)*`. Read `message-routing-spec.md` first — this spec assumes its
mechanism (the interception point, the bounded retry, the fail-open ladder) and states only what
differs.

## The problem

Owners lose track of which outstanding message a reply is for. A comment is sometimes a steer,
sometimes needs no response, and sometimes is an entirely separate train of thought; when several are
in flight, a reply that doesn't say which one it answers leaves the owner reconstructing the thread.
The natural fix — a standing instruction to *name what each reply is answering, at the top* — does not
hold.

**Measured on a production deployment, over one evening of Telegram turns after the assistant had
explicitly committed to the rule** (assistant-side, human-origin rows of `state/turns.jsonl`, marker
regex `\*?\(answering`):

```
complied:            26 / 54 = 48.1%
P(miss | previous miss) = 74.1%   (n = 27)
P(miss | previous hit)  = 26.9%   (n = 26)
```

Three findings shape the design:

1. **Misses cluster.** A 74.1% conditional-miss rate is the signature of autocorrelation within a
   run: once the model drops the line, the dropped form becomes the pattern it continues. This is
   evidence for clustering, not proof of a specific mechanism.
2. **Most compliant runs were length 1.** A reminder produces exactly one good reply and the habit
   collapses. Recovery is fragile, not partial — "remind harder" is not a fix.
3. **Reminders decay.** Each successive correction bought fewer compliant turns than the last. A
   further promise is not a countermeasure, which is why this needed a mechanism instead.

**The contrast that motivates the design:** `[[channel:PURPOSE]]` has near-total compliance over the
same kind of window — not through virtue, but because `channel_declare.py` **retries**: a reply missing
the line is bounced back before anything sends. This spec generalizes that machine to a second line.

## Design — no trigger, ever

**The reply-marker line is required on every Telegram reply, unconditionally.** No outstanding-message
count, no mid-turn ambiguity detection, no "optional when unambiguous" branch, and none should be added
later "as an optimisation" — its absence IS the design. An earlier draft proposed firing only when the
reference was genuinely ambiguous; the owner's decision was simpler: always. The reasons are on the
record because a future edit will be tempted to add the cleverness back:

- A rule phrased *"every time there is any ambiguity"* requires the model to self-observe an internal
  state, and a trigger on an internal state is exactly the trigger that silently fails to fire. An
  unconditional rule has no precondition to get wrong.
- `[[channel:PURPOSE]]` already proves a per-reply required line is tolerable in practice.
- A redundant "answering your last message" on a trivial exchange costs one clause; a missing one
  costs the owner the thread. The asymmetry is not close.
- Every line of trigger logic is a line that can decide "not required" and be wrong, silently, in
  exactly the direction that reproduces the failure being fixed.

**Content is the assistant's judgment; presence is mechanical.** The check verifies that a line
matching the pattern exists in the right position. It never judges whether the stated answer is
*correct* — the same presence-not-validity polarity as `message-routing-spec.md` decision A.

## 1. The line format

```
*(answering <what you're replying to> — <optional aside>)*
```

as the line immediately after `[[channel:PURPOSE]]` — or literally first, if the channel line is
itself missing (§3). Matched at position 0 of the reply **as it stands after channel extraction**:

```python
REPLY_MARKER_RE = re.compile(r"^\*\(answering\b.*?\)\*[ \t]*(?:\n|$)", re.IGNORECASE)
```

Non-greedy up to the first `)*`, so a parenthetical inside the content (`"...the second one))*"`)
still closes on the model's own closing asterisk rather than an earlier bare `)`. `(?:\n|$)` rather
than a mandatory newline: a corrective retry's answer (§3) may be the entire string with nothing after
it, and it must still match.

**Why this shape.** It is the organic form compliant replies already used, so it costs no new
convention. `*(...)*` is ordinary, inert Markdown — italicized, parenthesized — which matters for the
opposite reason it mattered for `[[channel:...]]`: that token hides behind unusual syntax so a leak
reads as broken plumbing, because it must never reach the owner; this line is *supposed* to look like
an ordinary italicized aside, because it is meant to be read.

## 2. THE ANSWERING LINE IS NOT STRIPPED

This is the one thing a later edit is most likely to get backwards. `[[channel:PURPOSE]]` is parsed
and removed before delivery (`message-routing-spec.md` §1) — it is plumbing. `*(answering ...)*` is
**left in the delivered text**, because it is written for the owner, not for the daemon.
`channel_declare.has_reply_marker()` is a pure read: it checks position 0 and never slices anything
out of `reply`.

**The consequence, and it is new relative to the channel mechanism:** because the line must reach the
owner, a corrective retry's own *text* can now flow into what gets delivered. The channel mechanism's
corrective never needs its text to survive — only the parsed purpose matters. Here, when the original
reply is missing the marker and a retry supplies it, that line is **prepended onto the
already-obtained substantive reply** before delivery. `extract_reply_marker_line` exists for exactly
this: it returns only the matched line — discarding anything the model adds after it despite being told
"ONLY that one line" — so what reaches the owner from a corrective is bounded to the one line they were
owed, never a second free-form message riding in on the side door.

## 3. One retry, reshaped — never a second one stacked beside it

`CHANNEL_DECLARE_MAX_RETRIES` stays `1` (`message-routing-spec.md` §8 fork 1) and is the SHARED bound
for both lines. The loop that checks `declared is None` now checks `declared is None or not
has_reply_marker(reply)`, and the one retry prompt it sends names whichever line(s) are missing:

```python
def retry_prompt(missing_channel: bool, missing_marker: bool) -> str:
    if missing_channel and missing_marker:
        return CHANNEL_AND_MARKER_RETRY_PROMPT_TEMPLATE   # names both, asks for both, in order
    if missing_channel:
        return CHANNEL_RETRY_PROMPT_TEMPLATE              # unchanged wording
    return REPLY_MARKER_RETRY_PROMPT_TEMPLATE             # names only the marker
```

**A reply missing both lines costs ONE round trip, not two.** The corrective is parsed once
(`extract_channel_declaration`), and whatever remains after any channel line is stripped is checked for
the marker (`extract_reply_marker_line`). Either, both, or neither piece may be recovered from a single
corrective; there is no second loop, no second send, and no second retry budget.

**Retry attribution is split, deliberately, though the retry itself is not.** `retries_used` (the field
`classify_outcome` and `message-routing-spec.md` §8 fork 3's 90%-rescue gate read) counts only
iterations where the **channel** was missing. A separate `reply_marker_retries_used` counts iterations
where the **marker** was missing. With at most one iteration, these are 0/1 attributions read off the
same iteration, not a second counting mechanism; §6 says why the split is necessary.

**Every exit from the loop still reaches a deliverable reply.** A dead corrective ask, retries
exhausted, or a non-Telegram channel: `reply` — obtained before any retry ran — is delivered, with the
marker prepended if a retry recovered it, without one if not. **A reply is never held back for a
missing marker.**

## 4. Scope — Telegram only, exactly like the channel line

`channel == "telegram"` gates the whole mechanism, for the same reason as `message-routing-spec.md`'s
scope clause: Discord and the cockpit have no retry path to hang this on. This is narrower than an
unqualified "always" — named here as an inherited scope, not a separate decision.

## 5. Grounding

Folded into the SAME channel-declare instruction slot the cold `GROUNDING` template carries —
`channel_declare.GROUNDING_INSTRUCTION_TEMPLATE` describes two required opening lines instead of one,
with no new template placeholder. The instruction is stated once, at cold grounding; the
machine-checked retry (§3), not a restated prompt, is what enforces compliance on a long-lived warm
session. The resume preamble is untouched: a resumed session already has the instruction in its
history. (The per-turn topic line *is* refreshed every turn, for an unrelated staleness reason —
`message-routing-spec.md` §8 fork 4.)

## 6. Logging — `channel-declare-log.jsonl`, extended

Three fields on the existing row (schema `seneschal.channel-declare-log/2`, additive — no existing
field renamed or removed):

| field | meaning |
|---|---|
| `reply_marker_required` | `true` whenever the row is written at all (Telegram-only, like the channel check) — explicit rather than implicit, so a future non-Telegram caller cannot silently mean something different by omitting it |
| `reply_marker_present` | the FINAL state after any retry — `true` if the marker reached delivery (first try or recovered), `false` if every attempt exhausted without one |
| `reply_marker_retries_used` | iterations where the marker specifically was missing — not the same field as `retries_used` |

**Why a separate counter rather than overloading `retries_used`:** `retries_used` feeds
`classify_outcome`'s `declared-after-retry(n)` string, which feeds the channel mechanism's phase-2 exit
gate — a rescue-rate measurement specifically about the **channel** declaration. A reply that declared
its channel on the first try but lacked only the marker would, under a shared counter, log
`retries_used: 1` — silently counting as a **channel** rescue it never needed, diluting that gate with
events it was never built to include. Splitting costs nothing and leaves the gate's arithmetic intact.

## 7. What this deliberately does NOT do

- **No semantic check.** Presence, never correctness. A `*(answering the wrong thing)*` line passes.
- **No second, independent retry mechanism.** §3.
- **No trigger, condition, or ambiguity classifier of any kind.** "Design," above.
- **No new grounding template placeholder** and **no change to the resume preamble.** §5.
- **No retroactive scoring** of rows written before the mechanism existed — no backfill.

## 8. Where "reuse the channel mechanism" is not quite exact

1. **The two lines are parsed under different contracts.** The channel corrective's prose is always
   disposable; the marker corrective's text becomes DELIVERED content. That is why
   `extract_reply_marker_line` is a function distinct from `extract_channel_declaration` rather than
   the marker riding the same code path — discard-after-reading vs. read-and-keep, sharing one loop.
2. **"Reshape the existing retry" means the ROUND TRIP, not the counter.** Reusing `retries_used` would
   have quietly corrupted the channel mechanism's exit gate (§6).
3. **The Telegram-only scope is inherited, not separately decided** (§4).

## 9. The empty-reply case

**The defect.** The drainer guards `reply is None` (a dead/hung session). A live, non-hung turn can also
return an **empty string** — typically when the turn's real work happened entirely in tool calls. That
guard does not catch it, so `reply` reaches the channel/marker machinery as `""`:
`extract_channel_declaration("")` → `(None, "")` and `has_reply_marker("")` → `False`. An empty reply
reads as missing BOTH lines, so the corrective retry fires; its prompt asks for exactly the two control
lines, so the corrective IS two lines — and the marker gets prepended onto an EMPTY `reply`. The owner
receives a bare `*(answering ...)*` header with nothing behind it, and the log records
`reply_marker_present: true`: the mechanism believes it succeeded. This was observed in production, not
merely hypothesized.

**The decision: when the substantive reply is empty, send NOTHING** — not the marker line alone, not an
apology, not an empty message. The point of the substance-or-silence design
(`substance-or-silence-spec.md`) is that not every inbound needs a reply. An apology would falsely
claim the turn failed; and this mechanism is about the SHAPE of a reply that exists, never a verdict on
one that doesn't.

**Where the guard sits, and why there, exactly once.** The drainer checks `not reply.strip()`
(whitespace-only counts as empty) immediately after the `reply is None` branch resolves — by then a
`None` reply has already become a non-empty apology/timeout string, so the empty check can only fire on
genuine empty model output — and strictly BEFORE `extract_channel_declaration`/`has_reply_marker` and
the retry loop. Any later and the retry is spent recovering two control lines for a reply that was never
going to have a body; any earlier and it would have to re-derive the `None` branch's non-emptiness
guarantee. **Deliberately NOT scoped to Telegram** — an empty reply on Discord or the cockpit is the
identical defect, even though the channel/marker machinery itself is Telegram-only.

**The bookkeeping, walked through rather than assumed.** Everything the drainer does before this point
already ran unconditionally and is untouched: the interleave turn window, the turn pointer, the cockpit
`turn_started` tee, and the record of the owner's own side of the exchange (it is real regardless of
what comes back). Turn-close bookkeeping runs in the turn's `finally` block and is unaffected. The
cockpit's `turn_done` frame comes from the raw stream `result` event *during* the send, so an empty turn
still closes cleanly in the trace panel, visibly empty. What the branch skips is only the machinery that
describes or delivers SUBSTANCE that was never there: the channel/marker retry, `deliver_reply` (and so
`mouth.record_assertion` and the assistant's side of the turn record — neither may claim something was
said), and `channel_declare.record_outcome`. The queue entry is popped and treated as **answered**, not
failed — nothing retries it, the same "popped, not spent, not delivered" shape `turn_suppression.py`
uses (`substance-or-silence-spec.md` §6) — and one daemon-log line marks the silent turn so it is
visible there.

**Whether `channel-declare-log.jsonl` gets a row — decided NO.** A row would have to state SOME
`reply_marker_present`/`outcome`, and every available value misstates a reply that was never sent:
`false` implies a reply existed and lacked the marker; `defaulted-main` implies a routing default was
applied to a message that was never routed. The honest fact is that this turn asked no routing
question. **This changes the phase-2 gate's population, not its arithmetic**: without the guard, an
empty Telegram reply contributes a `retries_used: 1` "rescue" that rescued nothing, flattering the
retry mechanism; with it, such turns are excluded, which is the correct denominator.

## Cross-references

- `message-routing-spec.md` — the channel-declaration mechanism this generalizes; its §1-§4 and §8
  forks are the baseline this spec assumes throughout.
- `substance-or-silence-spec.md` — the other case where a turn correctly delivers nothing.

## Vocabulary (for the next session that greps for this)

what am I replying to · reply marker · which message · outstanding messages · forcing function ·
instruction that doesn't stick · answering line · reply-marker line · empty reply · bare marker line ·
send nothing · silence · blank reply

## Router entry

**Status:** the marker check, extractor, combined retry prompt and log fields BUILT in
`channel_declare.py`; the drainer wiring and §9's empty-reply guard pending.

**What it decides:** A second required opening line on every Telegram reply, `*(answering <what you're
replying to>)*`, unconditional, with no trigger. It shares the channel line's single bounded retry (one
round trip covers both), is **not** stripped (it is written for the owner), counts its retries
separately so the channel gate stays clean, and an empty model reply sends nothing rather than a bare
header.
