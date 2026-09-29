# Topic mirroring — a copy of a message in a topic, without losing it from the main chat

**Status:** `SPEC-ONLY` — **phase 1 is a completed live measurement against the real Bot API and a
real private-chat topic; nothing in the daemon changed.** · **Owner:** the assistant.

**Scope.** An owner who uses Telegram topics (`../scripts/telegram_topics.py`) may want one subject's
chatter — say, a side project — findable in its own **`projects`** topic without losing the full,
unfiltered stream in the main chat: *"if I want to find something specific, I can just swap to that
topic."* Moving a message is impossible — `message_thread_id` is fixed at send time,
`editMessageText` only touches the bot's own messages, and there is no `getForumTopics`
(`telegram-capability-map.md` §2.1 ①). This document is about the only candidate primitive,
`copyMessage`/`forwardMessage`, **measured rather than assumed** — the capability map recorded
copy/forward as never exercised ("one chat"), and this is that measurement plus the design it
licenses. Not a document about which topics should exist, or about the classifier that would decide
what a given subject's chatter *is*.

## Phase 1 — what was actually measured

Against a live bot, a live private chat, and a freshly created topic (created by this measurement
through `telegram_topics.thread_id`, the same fail-open path every other topic goes through). The
source was one real plain-text message of the owner's (a short *"Yes, queue it, please."*). **Exactly
two messages were sent, both against that one source, one attempt each:**

| | `copyMessage` | `forwardMessage` |
|---|---|---|
| Landed in the topic | **yes** | **yes**, `is_topic_message: true` |
| API response shape | **`{message_id}` only** — Telegram's `copyMessage` returns a bare `MessageId`, not a `Message`; the copy's actual rendering is not visible in the API response at all | a full `Message` object |
| "Forwarded from" attribution | **none** — no `forward_origin` field exists on a copy by construction; this is the documented difference between the two methods, and the bare response confirms there is nothing to carry it | **yes, and it names the owner** — `forward_origin.type: "user"`, `forward_from` = the owner's own account. Forwarding **their own** message back into their own chat renders as *"Forwarded from <owner>"* |
| Author the message renders as | **the bot** — a copy carries no origin marker at all, so it reads exactly like an ordinary message the assistant sent, in the assistant's voice, saying words that were actually the owner's | **the owner**, explicitly, via the forward banner |
| Text preserved | the **raw Telegram text** — not the `(replying to: ...)`-prefixed string `turns.jsonl` records for the same turn; that prefix is `telegram_poll.extract_reply_to`'s synthesis, not part of what Telegram stores as the message body | same |
| Media / captions / formatting | **untested** — the source message was plain text with no entities, so this run says nothing about either method's handling of photos, captions, or HTML/Markdown entities. A future test needs a message that has some | same caveat |
| Cost | one `copyMessage` call | one `forwardMessage` call |

**Both methods work, unconditionally, on the one shape tested.** Nothing was refused, nothing
degraded, no fallback rung fired. That is the headline result: the Bot API primitive is not the
open question here — **which method, and what it renders as, is.**

**What was NOT measured, and is not invented here:**
- **Does it also show in the main chat?** Not independently re-observed for a copy or a forward.
  The owner-observed client behaviour for an *ordinary* topic send — visible in the topic **and** the
  main chat — is a client observation, not a Bot API guarantee, and both `copyMessage` and
  `forwardMessage` set `message_thread_id` through the exact same mechanism an ordinary send does.
  **Inferred to behave the same way, not confirmed independently.**
- **Does replying to the copy produce a usable `message_thread_id` on the way back in?** Not
  tested — it needs a real reply from the owner. `telegram_poll.extract_thread_id` reads
  `message_thread_id` off *any* inbound message uniformly (it does not special-case how the message
  in that thread was created), so a reply to a copy or a forward should carry the thread exactly as a
  reply to an ordinary topic message does. **A reasonable inference from the existing code path, not
  an observed one.**
- **What the API returns on failure.** Both calls succeeded on the first attempt; no error path
  (deleted source message, a message too old, a media type Telegram refuses to copy) was exercised.

## The primitive, stated plainly

**`copyMessage` is the right tool for a mirror, and `forwardMessage` is the wrong one, and the
measurement is why — not intuition about the method names.** A copy reads as though the assistant
said it; a forward reads as though the owner forwarded their own message to themselves, which is
genuinely confusing UI (Telegram has no concept of "forward this message you sent me, into a room
you're also in" — the banner it renders was designed for a message relayed between two different
people). **Neither carries any marker saying "this is a copy, not new."** That absence is not a
defect in either method; it is the shape a caller must design around. A mirrored message that is
indistinguishable from an original is the single biggest risk this feature carries, and it is
addressed below under duplication and failure posture — not by picking a different Bot API method,
because there isn't a better one.

## How a turn gets classified as belonging to a topic

Three signals exist, and they are not equally reliable:

1. **The thread the turn replied to.** If the owner is already inside the topic and replies there,
   `message_thread_id` on the inbound message is exact — the same signal a per-topic conversation
   cache keys on. **Reliable, structural, free.**
2. **The owner's explicit say-so** — "put that in projects," a tag, a directive. **Reliable by
   construction**: it is the owner's decision, not an inference about their decision.
3. **Content classification** — does this turn's text look like it belongs to the topic's subject?
   This is the only signal that is a *guess*, and it is the one a mirroring feature would lean on
   hardest, because most of what belongs in a subject topic is said from the main chat, unprompted,
   with no thread and no explicit tag. **Nothing in this repo has measured a topic classifier's
   accuracy.** **A wrong copy is cheap** (delete it, or leave it — see failure posture); **a wrong
   classification that makes the owner distrust the topic is not**, because the whole point of the
   topic is that they can trust it holds everything on its subject and nothing else. Signal 3 should
   therefore never run unattended in phase 2's first cut — it is a candidate, not a mechanism, until
   it has its own measurement, the way `router.py`'s classifier earned a shadow period before it
   decided anything.

## Duplication is the design, not a bug

**The main chat keeps everything. The topic is a filtered VIEW, built by copying into it — never a
move.** A future reader who notices a message living in two places and "fixes" it by deleting the
main-chat copy has broken the feature: the whole point is that the unfiltered stream stays whole and
the topic is where you go to find one thing inside it. `deleteMessage` on the *original* message must
never be part of this feature's implementation, on any path, for any reason — consistent with
`telegram-capability-map.md` §2.5 ⑤'s existing decision that deletion is for the owner's cleanup,
never for correcting what the assistant did.

## Volume and noise

**Every copy is a second notification for something the owner already saw once.** A topic that
mirrors live, unattended, on a content guess is a second buzz for every on-subject sentence in an
already-chatty channel — the same failure class the reminder stagger rule in
`../references/reminders-policy.md` exists to prevent generally. Phase 2 must not ship a rule that
turns the owner's phone into a duplicate of itself. Two shapes that avoid it, not mutually exclusive:

- **Copy only what the owner or the assistant explicitly marks** — signal 1 or 2 above, never
  signal 3 alone. This is the cheapest to build and the one signal 3's un-measured accuracy cannot
  cost trust on, because nothing fires without a human (the owner, or the assistant acting on an
  explicit in-turn decision) choosing it.
- **Batch, if content classification is ever used at all** — hold candidates and copy on a cadence
  (once a day, or once per idle gap) rather than in the same turn the notification would otherwise
  land, the same shape the inbound album hold uses for a different reason (quiet window, then flush;
  `telegram-inbound-spec.md` §6c). This does not fix a wrong classification; it only bounds how often
  a wrong one can buzz.

**Silent copying — no notification at all — is not offered as an option here.** Telegram's
`disable_notification` parameter exists and both `copyMessage`/`forwardMessage` accept it, but a
message that lands with no signal is a message the owner will not know to go looking for, which
defeats the *"I can just swap to that topic"* use case as much as never copying it would. If a future
build wants a quiet mode, it is a separate decision, not a default.

## Failure posture

**A failed copy costs the copy, and never the original message or the owner's reply.** This is the
same posture every other Telegram write in this tree holds (`telegram_send.py`'s classify-before-act
rule, `telegram_topics.py`'s fail-open-on-every-path). Concretely: the copy call runs strictly
*after* the original message has been delivered and processed normally — never gating, delaying, or
wrapping the primary send — so a `copyMessage`/`forwardMessage` failure (source message aged out,
the topic id gone stale, a transient error) is swallowed and logged, the shape
`telegram_topics.create_topic`'s failure handling already uses. **Nothing about mirroring may ever
become a reason a message doesn't reach the owner, or reaches them twice**, so the existing
`SEND_REJECTED`/`SEND_UNDELIVERED`/`SEND_AMBIGUOUS` classification applies unchanged to the mirror
call: only a rejected, provably-undelivered mirror may be retried or reported; an ambiguous one is
left alone rather than risking a second copy of the copy.

## OPEN — the owner's to decide

Nothing below is decided. Each is a real fork, not a formality:

1. **Opt-in per topic, or automatic?** Whether a topic mirrors by default once created, or only once
   the owner turns mirroring on for it specifically.
2. **Whose messages get copied — the owner's, the assistant's, or both?** A message the assistant
   composes on the topic's subject is its own text and easy to send with a topic already attached
   (no copy needed at all — that is just `--topic projects` at the original send, the mechanism that
   already exists). A message the *owner* sends from the main chat is the case that actually needs
   `copyMessage`, and it is also the more sensitive one to get an author-rendering wrong on (the
   measurement above: a copy of the owner's words shows with no marker that it came from them).
3. **Can an old thread be back-filled, and how far?** Whether a topic gets seeded from on-subject
   conversation that already happened in the main chat before the topic existed, or starts empty and
   only grows forward. Backfilling needs message ids for old messages, which — per
   `telegram-capability-map.md` — the Bot API gives no bulk way to enumerate; it would mean walking
   whatever this repo already records (`turns.jsonl`, the message map, past `getFile` results) rather
   than asking Telegram.

## Where this lives

**Not decided here, but scoped:** a `telegram_send.py`-level flag (`--copy-into TOPIC`, mirroring
the existing `--topic` seam) is the shape that composes with everything already built — resolved
lazily, advisory in every direction, costs nothing when unused — versus daemon logic that decides
*on its own* to mirror a turn, which is the only shape that could ever act on signal 3 (content
classification) without a human already having decided. Given the volume-and-noise argument above,
a phase-2 first cut should probably be the flag alone — signals 1 and 2 both resolve at a call site
that already exists (a reply's thread, an explicit mark) — with the daemon-classifier version
deferred exactly as `router.py`'s own classifier was, until it has a shadow period and a measured
accuracy.

## Summary

**Can a copy of a message land in a topic while the original stays in the main chat? Yes, measured
live**: both `copyMessage` and `forwardMessage` land in a real topic on the first try — but they are
NOT interchangeable. `copyMessage` carries **no attribution at all**, so it renders as though the
assistant said the owner's words; `forwardMessage` carries a *"Forwarded from"* banner naming the
owner, which is confusing UI for forwarding someone's message back into their own chat. **Duplication
is the design** — the main chat keeps everything, and deleting the original to "fix" the duplicate is
the regression. Of three classification signals, only content classification is a guess, and it
should not run unattended in a first cut. Every copy is a second notification, so phase 2 should copy
only on signals 1-2 or batch. A failed copy costs the copy, never the original. **Three open forks,
all the owner's**: opt-in per topic vs automatic, whose messages get copied, and whether an old
thread can be back-filled.
