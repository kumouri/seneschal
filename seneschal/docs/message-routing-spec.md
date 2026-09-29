# Message routing — a channel-declaration requirement on the assistant's reply path

**Status:** `PARTIAL(channel_declare.py BUILT — §1-§4's parse/strip/retry/fuzzy-match machinery + the
§7 log + telegram_topics.purpose_for_thread; the presence.py drainer wiring that runs it (phase 1) and
routes by it (phase 2) not yet landed; phases 3-4 unbuilt)`.

**Scope.** A structural forcing function on `presence.py`'s chat-turn reply path — the one that sends
the turn into the warm session and hands the result to `deliver_reply` — that makes it impossible for
a turn reply to leave the daemon without having named a Telegram destination, plus a secondary,
shadow-only hint classifier on `router.py` that suggests one. **These are two features, not one**, and
only the first is load-bearing; the classifier ships deciding nothing.

`seneschal/scripts/channel_declare.py` is the whole of §1-§4 plus the §7 log, covered by
`seneschal/scripts/test_channel_declare.py`. The drainer call site, the `deliver_reply` parameter, the
per-turn topic line in grounding and the cockpit `reply_preview` strip arrive with the daemon wiring;
until then the module is present and nothing calls it.

## The founding failure

With named Telegram topics configured (`seneschal/references/telegram-topics.example.json`), a long
status update about one project landed in the main chat although a topic existed for exactly that
kind of content. The mechanism to route it worked; the assistant simply never reached for it, because
nothing on the reply path asks. A reply defaults to wherever the question came from, and nothing ever
asks whether that is where the *answer* belongs.

## The design decisions — settled, not reopened

**A — the requirement is PRESENCE, never validity.** Refuse a send that names no channel. Refuse
nothing for naming a channel that turns out to be wrong. `telegram_topics.py`'s `TOPIC_MAIN_CHAT`
(`main`) is deliberately not a row in `TOPIC_NAMES` for exactly this reason — it keeps `--topic` free
of an argparse `choices` that would turn a mistyped purpose into a message that never arrives. This
forcing function checks that a destination was **named**, never that the name is **good**.

**B — an unknown channel resolves to main.** The same fail-open as `telegram_topics.thread_id`'s
unknown-purpose branch.

**C — a misspelling gets fuzzy-matched, cutoff 0.8.** `difflib.get_close_matches(name, TOPIC_NAMES,
n=1, cutoff=0.8)` — stdlib, no new dependency. Below threshold: main.

**D — a PreToolUse hook is the wrong placement.** The founding miss was an ordinary conversational
reply, not a Bash call running `telegram_send.py`, so a hook over tool calls would never have seen it.
The forcing function lives on the reply path itself, in `presence.py`.

## Where the forcing function goes

`presence.py`'s `drainer_task` is the sole consumer of the durable action queue. For a chat turn it:

1. Builds the prompt from the inbound text (and the inbound `topic` — a raw Telegram
   `message_thread_id`, or `None` for the main chat).
2. Sends it into the warm session — the one place the model's raw turn output exists as a string.
3. On a dead/hung session (`reply is None`), synthesizes an apology or timeout notice in its place.
4. Calls `deliver_reply(..., topic=topic)` — and **`topic` here is the INBOUND thread id from step 1**,
   unchanged, unexamined. This is the exact mechanism of the founding failure.

The forcing function sits between steps 3 and 4: after `reply` is known to be real, non-empty text and
before it is handed to `deliver_reply`. It replaces the raw inbound `topic` at that one call site with
a channel the model was made to name.

## 1. The declaration format

A fixed-position, first-line control token — never a scan of the whole message:

```
[[channel:PURPOSE]]
```

as literally the **first line** of `reply`, matched only at position 0 (`CHANNEL_DECLARATION_RE`).

**Why this shape and not a hashtag or an XML tag.** A leading `#purpose` risks rendering as a live,
tappable Telegram hashtag entity if a stripping bug ever lets it through — the one failure mode this
feature must be most paranoid about, since the marker must never reach the owner. `[[…]]` has no
Markdown or Telegram-entity meaning; a leak reads as inert, obviously broken plumbing rather than
blending into prose. It is also visually distinct from the codebase's existing **input-side**
bracketed hint lines (the fable hint, the arrival marker, the router-hint prefix — all *prepended to a
prompt before the model sees it*), so a reader of the code never confuses "a hint the assistant
received" with "a declaration the assistant produced." The inline capture tags (`<todo>`,
`<tomorrow>`) are deliberately *not* reused: they are scanned anywhere in a message and meant to
survive to the reader; this token must do the opposite.

**Position 0 only.** A `[[channel:…]]`-shaped string mid-message, inside a code fence, or as a quoted
example (the assistant explaining this very feature) is never parsed as a declaration and never
stripped.

**Stripping.** `extract_channel_declaration(reply) -> (purpose | None, reply)` is a pure function: on a
match it returns the lower-cased purpose and `reply` with the matched line **and exactly one following
newline** removed; on no match it returns `(None, reply)` unchanged. Called once, so it runs
identically whether `reply` is model output or a synthesized apology/timeout string (the latter never
match — they are the daemon's own words, and the owner is owed no channel choice about them). The
**stripped** `reply` — never the raw one — is what flows into `deliver_reply`, the thread tail, the
turn record (`turns.py`), `mouth.record_assertion` and the cockpit tee's `reply_preview`. There is
exactly one interception point; nothing downstream of it ever sees the token.

## 2. What "report an error to the model" means concretely

There is no tool-call error surface here (D) — no exit code, no `permissionDecision`. The daemon's only
channel to the model is another turn. So a refusal is a plain natural-language message, sent into the
**same** warm session the turn already used, read by the model exactly as it reads an owner message or
a router hint line: `CHANNEL_RETRY_PROMPT_TEMPLATE`, which names the missing line, lists the known purposes (or
`main`), and asks for ONLY that one line.

The corrective response is parsed with the **same** `extract_channel_declaration` — one parser, one
test surface. Its prose (if the model adds any) is **discarded**: it never reaches `deliver_reply`,
`turns.jsonl`, or `mouth.record_assertion` — it is plumbing, not a second reply. It still rides the
turn's own event tee (visible in the cockpit trace panel under the same `turn_id`, for debuggability),
but it is never delivered and never counted as a queue attempt — the durable-queue "one pop, one
delivered reply" invariant is untouched.

## 3. The hard rail: a RETRY, never the MESSAGE

```python
CHANNEL_DECLARE_MAX_RETRIES = 1   # §8 fork 1

declared, reply = extract_channel_declaration(reply)
attempts_left = CHANNEL_DECLARE_MAX_RETRIES
while declared is None and attempts_left > 0 and channel == "telegram":
    attempts_left -= 1
    corrective = await asyncio.to_thread(
        functools.partial(state.session.send, cd.retry_prompt(True, False), on_event=on_event))
    if corrective is None:        # the corrective ask itself hung/died — fail open, don't retry further
        break
    declared, _ = extract_channel_declaration(corrective)
resolved = resolve_purpose(declared)   # B/C — always returns a real purpose, never None
```

**Every exit from this loop reaches `resolved`, and every value of `resolved` is deliverable.**
`resolve_purpose` (§4) always returns a string, worst case `"main"`. The loop is bounded on three
independent axes, and any one of them ending the loop is safe:

- **Retry count** (`CHANNEL_DECLARE_MAX_RETRIES`) — the model keeps declining to declare.
- **A dead corrective ask** (`corrective is None`) — the warm session hung or crashed answering the
  channel-check itself. This must never be read as "the substantive reply also failed" — `reply` is
  already obtained and still gets delivered. A hang here is caught by the same per-`send()` watchdog
  every call already carries, so no new timeout mechanism is needed; if the session is left unusable,
  the existing session-teardown handling runs — but only *after* `reply` is queued for delivery, so it
  can only cost the *next* turn's warm-session continuity, never this one's answer.
- **Non-Telegram channel** — see scope, below: no routing decision exists to retry for.

**What must never happen:** the forcing function costing the *substantive reply*. A crash mid-retry, a
restart between the original send and a corrective ask, a model that keeps omitting the token, a token
stripped by some other layer — none may turn into "the reply is lost" or "nothing is sent." The
mechanism can only spend up to `CHANNEL_DECLARE_MAX_RETRIES` extra short round trips and then fall
open to `main`. A downstream `deliver_reply` failure is handled exactly as before (requeue, backoff,
session drop), because by then `resolved` is a plain string with no further dependency on this
machinery. **A restart between obtaining the reply and delivering it** is not a new failure mode: the
durable queue entry is still un-popped, and the retried turn re-runs the whole exchange, including a
fresh declaration.

**Never spend the retry recovering a reply that was never there.** An empty (or whitespace-only) model
result would otherwise reach this mechanism as an ordinary undeclared reply, fire the retry, and
deliver the corrective's own control line as the entire message. The drainer must intercept an empty
reply BEFORE this mechanism runs and send nothing (`reply-marker-forcing-function-spec.md` §9). Such a
turn writes no row to the §7 log — not `defaulted-main`, not any other outcome — because it never asked
a routing question. That narrows the log to what it is meant to measure; it is a correction to the
phase-2 gate's denominator, not to its threshold.

## Cross-check against `telegram_send.py`'s SEND_REJECTED / SEND_UNDELIVERED / SEND_AMBIGUOUS

This mechanism is entirely upstream of that classification and does not duplicate or interact with it.
It resolves *which purpose string* applies; resolution against the topics table and the existing
fail-open ladder (unreachable `getMe`, topics off, a stale thread rejected and forgotten, a creation
failure — all landing at the main chat) is `telegram_topics.py`'s, exactly as its module docstring
documents.

`deliver_reply` gains a new parameter, `channel_purpose: str | None = None`, distinct from — and, for a
chat-turn reply, used *instead of* — its existing numeric `topic` parameter (which stays as it is for
every other caller: job pushes, the wake line, the dead-letter notice). The drainer passes
`channel_purpose=resolved_purpose`; `resolved_purpose` is computed unconditionally and stays `None` off
Telegram, so nothing changes for Discord or the cockpit.

**Resolution should run IN-PROCESS, not via `telegram_send.py --topic`.** A subprocess resolving purely
by purpose has no notion of what the inbound thread was, so its only fallback on failure is `main`,
silently discarding a perfectly good inbound thread the message already had a home in. Resolving where
`topic` (the inbound thread) is in scope — reading Telegram config in-process, a resolve rather than a
send, so it carries none of the duplicate-send risk that keeps an actual send behind
`telegram_send.py` — lets a resolution failure (an unknown purpose, topics off, an unreachable `getMe`,
an unreadable state file, a failed creation) fall back to the **inbound** thread first, and only then
to `main`. Under `--stub-send` it reads only `telegram_topics.stored_thread_id` (a plain state-file
read, never an API call or a topic creation), so the offline harness can exercise the fallback
deterministically. The three send classes still classify exactly as before; a *rejected* resolved
thread still falls to `main`, as a rejected inbound thread always has.

**Scope.** This applies to chat-turn replies. Reminder nudges, job-completion pushes, Watch escalations
and the Brief are daemon-composed and already route explicitly (`--topic PURPOSE`, with no model in the
loop to declare anything) — **they are OUT of scope**, unchanged.

**Discord and the cockpit.** `telegram_topics.py` is Telegram-only — there is nothing to route a
Discord or cockpit reply *to*. The parse-and-strip half (§1) still runs on every channel, so a stray
declaration line can never leak regardless of surface, but the bounded retry (§3) is gated
`channel == "telegram"` — retrying for a routing decision that has no effect is pure overhead.

## The inbound-thread interaction

When the owner writes *from inside* a topic, the inbound `message_thread_id` is exact and reliable —
`topic-mirroring-spec.md` rates "the thread the turn replied to" as the strongest of its three
classification signals. Does replying inside a topic already satisfy the requirement?

**No — the requirement still applies, unconditionally, and auto-satisfying it is the wrong call.** The
founding failure's shape is "the correct destination is not always where the question arrived." If
replying from inside a topic exempted a turn from declaring, the exemption would fire on exactly the
cases that happen to already be right and do nothing for the cases that are wrong — and worse, it
relocates the *same* defect: a reply written from inside one topic about something unrelated (the
owner happened to still be typing there) would go **back into that topic unquestioned**, because the
model was never made to look. The value of a shape requirement is that a choice is made **every
time**; an exception for "already sitting somewhere plausible" reopens the hole this spec closes.

**A disagreement between the inbound thread and the declared channel is a signal to log, not an
error.** Most replies belong where they were asked. When they *disagree*, that is useful data: a high
rate of "declared differs from inbound, and the declared one was right" is evidence the feature earns
its cost; inconsistent redirection for similar content would suggest confusion. §7's log records
`(inbound_purpose, declared_purpose)` on every turn for exactly this — logged, never acted on beyond
routing to the declared value per A.

## 4. The fuzzy-match design and its hand-check

```python
CHANNEL_FUZZY_CUTOFF = 0.8

def resolve_purpose(declared: str | None) -> str:
    if declared is None:
        return "main"
    declared = declared.strip().lower()
    if declared in TOPIC_NAMES or declared == TOPIC_MAIN_CHAT:
        return declared
    match = difflib.get_close_matches(declared, list(TOPIC_NAMES), n=1, cutoff=CHANNEL_FUZZY_CUTOFF)
    return match[0] if match else "main"
```

`declared.strip().lower()` before matching is load-bearing, not decoration — see below.

**The hand-check, run against the shipped example purposes** (`pull-requests`, `decisions`,
`reminders`), using `difflib.SequenceMatcher` directly:

| input | best match | ratio | passes 0.8? |
|---|---|---|---|
| `decission` | decisions | 0.889 | yes |
| `decison` | decisions | 0.875 | yes |
| `decisons` | decisions | 0.941 | yes |
| `decision` (singular) | decisions | 0.941 | yes |
| **`desicions`** | decisions | **0.778** | **no** |
| `reminder` (singular) | reminders | 0.941 | yes |
| `remindrs` | reminders | 0.941 | yes |
| `remider` | reminders | 0.875 | yes |
| `pull-request` (singular) | pull-requests | 0.960 | yes |
| `pull requests` (space) | pull-requests | 0.923 | yes |
| `pullrequests` (no hyphen) | pull-requests | 0.960 | yes |
| `prs` (abbreviation) | pull-requests | 0.375 | no |
| `main` / `mian` / `amin` | (best real-topic match) | ≤0.462 | no |

**Findings, not estimates.**

1. **Case sensitivity is a real trap.** A topic's *display title* is usually capitalized, and the
   model may echo the title it sees rather than the internal key. `Pull Requests` scores **0.769**
   against `pull-requests` — below the cutoff — purely from case and a space; shorter keys lose even
   more per changed character. An un-normalized comparison would wrongly default such a reply to main.
   `.strip().lower()` before matching is therefore part of the design, not a follow-up.
2. **`desicions` — a plausible transposition of `decisions` — scores 0.778**, just under the cutoff,
   even normalized. It falls to `main`. That is not a defect — B makes `main` the safe landing for
   anything the fuzzy match can't confidently place — but it is the kind of "short strings behave
   badly under ratio matching" risk worth checking by hand. No action follows: `main` is no worse than
   an undeclared reply, and lowering the cutoff raises the risk of a wrong cross-purpose match.
3. **No cross-purpose collision among the shipped names** — every pairwise ratio is far below 0.8, so a
   typo of one purpose cannot be fuzzy-matched into a *different* one. An owner adding purposes should
   re-run this check: two purposes with similar names are the one way this mechanism could land a
   message on the wrong topic.

**Why the fuzzy match cannot lose or block a message:** it only runs *after* a channel has already been
declared — a guess about spelling, made once a choice has been made. Its worst case is `main`, where an
undeclared reply already goes.

## 5. The firing-rate argument

An always-on check has to justify its firing rate. Content-guessing detectors that fire on a large
share of outbound messages with a high false-positive rate get turned off, and deserve to be.

**This one is a different kind of object.** A heuristic detector reads a message's content and guesses.
This forcing function makes **no claim about content at all**. It asks one binary, structural question:
*does the first line of this string match a fixed regex?* There is no ambiguous case for it to be wrong
about, so it has **no false-positive rate** — which is what lets it run on **every** reply.

The fuzzy match (§4, C) is the one piece that *is* a guess — but it guesses about a **typo**, only after
a channel was already named (never deciding *whether* to act), and its worst case is `main`.

## 6. The hint classifier — `router.py`'s topic arm

`seneschal/scripts/router.py` already has the shape this needs: a local Ollama classifier reading one
inbound message, emitting a strict-JSON verdict, **never raising** (any failure returns the safe
fallback), with existing arms — the triage arm (`classify`, trivial vs escalate) and the fable arm
(`classify_fable`, standard vs fable-level — reaches the warm session as a prompt-only hint line, never
a command). **A topic arm is another arm of this exact shape — no new architecture.**

```python
TOPIC_SYSTEM_PROMPT = """\
You are the assistant's front-door TOPIC-HINT classifier. The owner uses named Telegram topics to keep
some kinds of conversation findable. You do NOT reply and you do NOT decide anything — you only
SUGGEST which topic a REPLY to one inbound message plausibly belongs in, so a downstream forcing
function can offer it as a hint. Nothing here is ever applied automatically.

Return STRICT JSON, exactly these keys and nothing else:
  {"verdict": "topic" | "none",
   "category": <one of the configured purposes> | null,
   "confidence": <number 0.0-1.0>,
   "reason": "<one short clause>"}

Bias HARD toward "none". When in any doubt, say none. Abstaining means none.
"""


def classify_topic(message: str, cfg: dict | None = None, timeout: int = 20) -> dict:
    """Never raises. Safe fallback is `{"verdict": "none", "category": None, ...}` — the same
    abstain-is-safe shape as `classify`/`classify_fable`."""
```

**Safe fallback: `none`/`null`.** Ollama unreachable, malformed JSON, an unrecognised category,
confidence below the router's threshold — all abstain rather than guess.

**It ships in SHADOW first. Non-negotiable.** The router's own first phase logged and decided nothing
until it earned the right to, and `topic-mirroring-spec.md` names that bar as the one a topic
classifier must clear. The topic arm logs to the existing `router-log.jsonl` with `"arm": "topic"` and
— in shadow — **feeds nothing back into the forcing function**: it never pre-fills a declaration, never
skips a retry, never changes where a message is delivered.

**What ends the shadow phase.** `topic-mirroring-spec.md` rates content classification a *guess*
because nothing has measured a topic classifier's accuracy. The forcing function supplies that
measurement for free: **every reply, once phase 1 is live, carries a ground-truth declared purpose.**
The topic arm's shadow verdict can be scored against it automatically, with no hand-labelling. The exit
criterion — an accuracy floor over a stated window — is a judgment about acceptable wrongness and is
the owner's (phase 4); it is distinct from §8 fork 3, which gates the forcing function's own
phase 1→2 move.

**The composition:** the forcing function guarantees a choice gets made; the classifier only ever
improves *which* choice, and can never be blamed for a channel the assistant declared itself.

## 7. Phase plan

**What ships now.** `seneschal/scripts/channel_declare.py` is §1-§4 plus this section's log
(`record_outcome` → `state/channel-declare-log.jsonl`, outcomes `declared-first-try` /
`declared-after-retry(n)` / `fuzzy-matched(from→to)` / `defaulted-main`, plus the inbound thread's own
resolved purpose), the grounding instruction text and the per-turn topic line helper
(`current_topic_line`); `telegram_topics.py` carries `purpose_for_thread`, the reverse lookup §8 fork 4
needs. Tests: `seneschal/scripts/test_channel_declare.py`.

**Phase 1 — the forcing function, observe-only (daemon wiring pending).** The drainer runs the
extraction, the bounded retry and the log call between the reply being obtained and `deliver_reply`;
grounding carries the declare instruction and a per-turn topic line (Telegram only, refreshed every
turn per §8 fork 4); the cockpit's `reply_preview` strips the same marker. **The stripping is
unconditional from day one** — the marker must never reach the owner — but `deliver_reply` keeps
routing by the inbound thread; `channel_purpose` is computed and logged, not yet acted on. This
validates retry convergence and fuzzy-match behaviour against real traffic before anything about where
a message lands can change.

**Phase 2 — the forcing function, live (daemon wiring pending).** Gated on §8 fork 3's exit bar. The
drainer passes `channel_purpose=resolved_purpose` on every Telegram turn, and **the declared, resolved
purpose wins over the inbound thread unconditionally**, per the inbound-thread section above. The case
this closes is concrete: a reply to a button tap arrives with no inbound thread, so an observe-only
call site delivers even a correctly-declared reply to the main chat. The thread-tail continuity cache
follows the same destination (a state-file echo of the resolution already performed, never a second
network call), so a cold spawn is grounded on the topic the reply actually landed in. Tests to bring
with the wiring: routing follows the declared purpose, not the inbound thread; a declared `main` from
inside a topic goes to the main chat; a resolution failure falls back to the inbound thread, then main;
Discord never gets a channel purpose.

**Phase 3 — the topic-hint arm, shadow.** §6's classifier, scored automatically against the declared-
purpose log. Decides nothing.

**Phase 4 — the topic-hint arm, gated.** Only on the owner's decision, once phase 3's accuracy is
known: the hint may ride the corrective-retry prompt as a suggestion — **never auto-applied, never a
substitute for the model's own declaration.** Not designed here.

## 8. Forks — all four decided

Each keeps its original framing — the record of *why* the question was hard — with the decision
appended. Two were decided by the owner and two by the assistant while writing the spec; the latter are
overturnable by the owner at any time.

1. **`CHANNEL_DECLARE_MAX_RETRIES = 1`, or a different number?** One extra round trip is cheap and
   converges fast; further retries after a plain-language correction have diminishing odds and start to
   resemble "write it down harder," which does not bind. **Decision (assistant, overturnable): stays
   `1`.** Phase 1's log is the data that would justify a different number; choosing one now would guess
   ahead of the measurement.

2. **Should the corrective retry run on the turn's own model dial, or a forced cheap/fast one?** The
   corrective ask is small, but forcing a second model into the mix mid-turn is new complexity.
   **Decision (assistant, overturnable): the turn's own model dial.** The only argument for a second
   model is cost, and on a subscription-billed CLI the marginal cost of one short round trip does not
   justify the extra moving part.

3. **The phase-1→phase-2 exit bar.** **Decision (owner): the retry must rescue 90%+ of undeclared
   replies over a 7-day window** before phase 2 flips to live routing. The residual tail lands in
   `main`, which is where those replies would land anyway — the tolerated wrongness is the status quo,
   not a regression. Rejected: a **zero mechanism-caused main-chat sends** bar (a model will
   occasionally not emit the token, so it may never clear and could stall the feature indefinitely),
   and **setting the bar after reading the log** (a bar chosen after seeing the data is one you can
   talk yourself into).

4. **Whether — and how — the model is told which purpose the CURRENT inbound thread resolves to.**
   Without it, a declaration is a guess made blind about where the conversation currently sits.
   **Decision (owner): refreshed every turn** — the same treatment the clock line gets — not cold
   grounding only. Refreshed every turn it can never go stale, and a stale wrong topic looks exactly
   like a correct one. The derivation is `telegram_topics.purpose_for_thread(state_dir,
   message_thread_id)`.

## Router entry

**Status:** `channel_declare.py` BUILT (§1-§4 + the §7 log); the drainer wiring for phases 1-2 pending;
phases 3-4 unbuilt.

**What it decides:** A structural forcing function on the chat-turn reply path that makes it
impossible to send a Telegram reply without declaring a destination (`[[channel:PURPOSE]]`, position 0,
stripped before delivery), plus a shadow-only `router.py` hint arm. Presence, never validity; unknown
resolves to `main`; a misspelling fuzzy-matches at a 0.8 cutoff; a bounded retry that can never cost
the substantive reply; a shape check with no false-positive rate, which is what lets it run on every
reply.
