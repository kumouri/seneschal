# Telegram inbound enhancements — spec

**Status:** `PARTIAL(§2-§5 and §3.6 BUILT; §6a edits, §6b the question picker and §6c albums BUILT in telegram_poll.py and telegram_ask.py, their presence.py wiring pending; Phase C §3.4 deferred)` —
§2 attachment intake (PR 1), §4 reply-context (PR 2), §3 reactions Phase A+B (PR 3), §5 backlog-ack
(PR 4) and §3.6 custom-emoji resolution (PR 5) are all **built** end to end — see §7. For §6a message
edits (PR 6), §6b the question picker (PR 7) and §6c albums (PR 8), the poller half
(`telegram_poll.py`: `edited_message`, `callback_query`, `media_group_id`) and the picker module
(`telegram_ask.py`) are built; the daemon half (`presence.py`: the edit/queue rewrite, callback
resolution and the album hold) lands with the daemon-wiring port, and until it does those inbound
kinds are extracted but not acted on. **Phase C (§3.4) remains ask-high and unbuilt** by decision.
**Author:** the assistant, on the owner's ask (2026-07-16); §6a-§6c added later, also on the owner's
ask.
**Scope:** the Telegram *inbound* path only. Outbound (`send_telegram`) is untouched.
**Autonomy:** every feature here is **act-low** to *build* (local ETL over the owner's own bot's inbound); the
only ask-high surface is if a reaction is ever wired to *approve an outbound draft* (see §3.4).

---

## 0. Why

Three gaps surfaced in one night:

1. **Attachments vanish.** The owner sent a health-export zip over Telegram; it was silently ignored.
   Root cause found live: the poller only ever reads message **text**, so any file arrives as an empty
   message and is dropped before it reaches the warm session.
2. **Reactions are invisible.** The owner wants a 👍 on a nudge / ❤️ on a note to *mean* something (a
   lightweight ack / bit of warmth) — raised 2026-07-16. Today the daemon doesn't even ask
   Telegram for reaction updates.
3. **Reply-threading has no context.** When the owner swipe-replies to an earlier message, the quoted
   message isn't threaded into the warm session, so they have to restate what they're replying to.

A fourth, related reliability item (the post-restart "got your N — working through them" ack) is captured
as a **companion** in §5 — it's Telegram-inbound UX and it's what made tonight's crash *look* like a
dropped-message loop.

---

## 1. Current state & exact choke points

> **Corrected during PR 1 (2026-07-16).** The draft below claimed extraction was duplicated between
> `sentinel.py` and `telegram_poll.py`, and therefore that `allowed_updates` had **two** sites to change.
> It doesn't: `sentinel.poll_telegram` is a thin **subprocess wrapper** that shells out to
> `telegram_poll.py` and parses its JSON. There is exactly **one** extraction site and **one**
> `allowed_updates` (`telegram_poll.py`). §2.3 and §6 are corrected to match; §3's "both `get_updates`
> sites" means the single one.

All Telegram inbound extraction happens in the standalone CLI **`seneschal/scripts/telegram_poll.py`**.
**`seneschal/scripts/sentinel.py`**'s `poll_telegram` is a subprocess wrapper around it (it owns the
offset-file path and the timeout budget, not the parsing). The CLI:

- Requests **only** `allowed_updates=json.dumps(["message"])` in `getUpdates`
  (`telegram_poll.py:76`). → Telegram never *sends* `message_reaction` updates, so reactions can't arrive
  even in principle.
- Builds each message as **text-only**: `"text": msg.get("text", "")` (`telegram_poll.py:132`). No
  `document` / `photo` / `voice` / `audio` / `caption` / `reply_to_message` is read.

Then in **`seneschal/scripts/presence.py`**, `telegram_task` (line ~971) filters the result down to
text-bearing items and **drops everything else**:

```python
new_inbound = [("telegram", (m.get("text") or "").strip(), 0)
               for m in res.get("messages", []) if (m.get("text") or "").strip()]
```

So there are **two** places a non-text update dies: the extraction (never captured) and this filter
(discarded even if captured). Both must change.

**Durability invariant to preserve:** `poll_telegram(..., commit=True)` advances the offset for
*everything* it fetched (including non-message updates — `telegram_poll.py:120-121`). Any new handling
must stay inside that already-committed batch so a message is never fetched-then-lost on a crash
(daemon invariant 1).

---

## 2. Feature — Attachment intake

### 2.1 Behavior
An inbound `document` / `photo` / `voice` / `audio` / `video` is downloaded to a local inbox and the warm
session is handed **the local path + any caption**, so the assistant can respond to the file in context.

**General-purpose, no per-type special-casing** (owner decision, 2026-07-16 — Q1 resolved). This is *not*
built around the health-zip flow (big data doesn't come this way — see the 20 MB cap in §2.4). The feature
is simply: **any attachment becomes something the assistant can see and respond to.** The warm session decides what
to do with it conversationally (open it, summarize, run a tool, or just acknowledge) — the daemon never
auto-runs a tool on an inbound file.

### 2.2 Data flow
1. Extraction (`poll_telegram`) detects a media field on the message and records, per message:
   `kind` (`document|photo|voice|audio|video`), `file_id` (for `photo`, the **largest** `PhotoSize`),
   `file_name`, `mime_type`, `file_size`, and `caption`.
2. A new helper `telegram_get_file(token, api_base, file_id, dest_dir)` in `sentinel.py`:
   - calls `getFile(file_id)` → `result.file_path`,
   - downloads `https://api.telegram.org/file/bot<token>/<file_path>` (stdlib `urllib`),
   - writes to **`state/inbox/<utc-stamp>-<safe-name>`**, returns the local path.
3. `telegram_task` builds an inbound item whose text is a synthesized line, e.g.
   `[attachment: document "samsunghealth_2026-07-15.zip" saved to state/inbox/…] <caption>`
   so the warm session sees a real, actionable message instead of an empty string.

### 2.3 Files — *as built (PR 1)*
- `telegram_poll.py` — the one extraction site: `extract_media` + `safe_filename` + `telegram_get_file` +
  `fetch_attachment`, behind a new `--download-dir`. Also `prune_inbox` + a standalone, offline
  `--prune-days` GC mode (§2.4 retention; the data's owner prunes it, as with `presence_import.py`).
- `sentinel.py` — `poll_telegram` gains `download_dir` and passes it through. The download therefore runs
  *inside* the poll subprocess — i.e. already in the caller's worker thread, off the event loop, and
  inside the batch whose offset it commits, with no extra plumbing. Downloading polls get their own
  timeout budget (`TELEGRAM_DOWNLOAD_BUDGET_SEC`) since a fetch rides along.
- `presence.py` — `telegram_task` no longer drops non-text; `telegram_inbound_text()` builds the
  synthesized line the warm session reads.
- `state/inbox/` — created on demand. **No `.gitkeep`:** `seneschal/state/*` is already gitignored wholesale
  (only `README.md` + `*.example.*` are tracked), so a keeper file would need a gitignore negation to
  exist at all — runtime dirs here are made by code and documented in `state/README.md`, like `sessions/`
  and `archives/`.
- `seneschal/modes/dream.md` — step 2 calls the inbox sweep.

### 2.4 Edge cases & guardrails
- **20 MB cap.** The Bot API's `getFile` download tops out at **~20 MB**. Health-export zips can exceed
  that. On a too-large file, **don't fail silently** — reply: *"That's >20 MB, above Telegram's bot
  limit. Drop it on the machine and I'll `health_import.py --jsons` it."* (This is exactly why the
  local-file path stays primary for big health data — the attachment path is a convenience, not the
  replacement.)
- **Path-traversal / name safety.** Sanitize `file_name` (strip directories, allow `[-\w. ]`, fall back
  to `<kind>-<file_unique_id>`). Never trust the client name for the write path.
- **Allowlist.** Downloads only for chats already passing `TELEGRAM_ALLOWED_CHAT_IDS`.
- **Fail-open.** Any download error → log + a plain "couldn't fetch that file" reply; the text pipeline
  is untouched.
- **Retention.** Nightly Dream prunes `state/inbox/` older than N days (mirror the `--prune-days 30`
  pattern used elsewhere).

### 2.5 Tests (stdlib unittest, `test_telegram_*`)
- extraction picks the largest photo size; captures `caption`; handles a message with *no* media.
- name-sanitizer rejects `../`, absolute paths, control chars.
- size-cap branch produces the "drop it on the machine" reply, no download attempted.
- `telegram_task` enqueues an attachment line (mock `telegram_get_file`); a caption-only text message
  still flows unchanged.

---

## 3. Feature — Reaction awareness

### 3.1 Behavior
A reaction the owner adds to a message (👍 on a nudge, ❤️ on a note, etc.) is delivered to the daemon and
mapped to a lightweight intent.

### 3.2 The one required switch
Add `"message_reaction"` to `allowed_updates` in **both** `get_updates` sites. Telegram delivers
`message_reaction` (`MessageReactionUpdated`: `chat`, `message_id`, `user`, `old_reaction`,
`new_reaction[]`) — but **only when explicitly requested** (off by default). In a private chat with the
bot, the user's own reactions are delivered without admin rights.

### 3.3 Extraction & mapping
- `poll_telegram` emits a reaction item: `{kind: "reaction", chat_id, message_id, emoji, from}`
  (emoji = the added `ReactionTypeEmoji.emoji` in `new_reaction` not present in `old_reaction`).
- The reaction→intent map — **the owner's authoritative set (2026-07-16)**; config in
  `state/telegram-reactions.json`, seeded from `.example`:
  - 👍 → **ack** — yes / confirm / accept.
  - ❤️ → **liked** — positive feedback on the *response itself* (warmth). No action; may later feed a
    response-quality signal.
  - 👎 → **reject** — no / **drop a held draft** / don't accept. (Rejecting/dropping is **act-low** —
    it's not outbound. Distinct from Phase C, which is 👍-*approving a send* = ask-high.)
  - ⏰ → **snooze** — more time / bring it back later; on a reminder nudge = snooze it.
  - 🤚 → **hold** — wait on it ≥ 1 day and **don't resurface unless asked**. (Stronger defer than
    snooze.)
  - ❔ → **elaborate** — explain / tell me more / ask me more.
  - any other emoji → **note** — thread as context, no action.

  > **Amended 2026-07-16 (owner decision, after the constraint below surfaced): in-set aliases added, originals
  > kept.** ⏰ / 🤚 / ❔ are **not** in Telegram's allowed reaction set — verified against the Bot API's
  > `ReactionTypeEmoji` list — so the picker never offers them and those three can never fire. They stay
  > mapped (they record the intended meaning, and cost nothing), and each gains **in-set aliases that
  > actually work**: **😴 / 🥱 → snooze**, **🤝 / 🙏 → hold**, **✍ / 🤔 → elaborate**. Many-to-one is fine —
  > the map is a plain emoji→intent lookup. 👍 / ❤ / 👎 were in the set all along and are unchanged.

  **Handling:** most intents resolve at the **warm-session (LLM) tier** — thread
  `[the owner reacted <emoji> = <intent> to: "<quoted reacted-to message>"]` in and let the assistant act in context
  (drop the draft, snooze/defer the item, elaborate, etc.). The **daemon-side automated** path stays only
  for 👍→ack **on a tracked reminder nudge** (§3.4B). ⏰/🤚 on a reminder should snooze/defer that row;
  🤚 also suppresses resurfacing for ≥ 1 day.

### 3.4 Wiring (staged — start read-only)
- **Phase A (observe):** thread the reaction into the warm session as context
  (*"[the owner reacted 👍 to your last message]"*) — no automated action. Ships first; zero risk.
- **Phase B (ack + affirm):** two act-low cases (owner decision, 2026-07-16 — Q3 resolved: *reminders **and**
  "👍 a question = yes"*):
  - **Nudge ack** — a 👍 on a **reminder nudge** runs the normal ack path (the store `done` write +
    `reminders_dequeue.py`). Needs the daemon to remember which `message_id` was which nudge (a small
    `state/telegram-message-map.json`: `message_id → {kind: "nudge", reminder_id}`, written when a nudge
    is sent).
  - **Question affirm** — a 👍 (or other `ack`-mapped emoji) on an **assistant message that posed a yes/no
    question** is interpreted as **"yes."** "Was the reacted-to message a question" is a judgment call →
    handle it at the **warm-session (LLM) tier**: thread the reaction *and* the quoted reacted-to text in
    (`[the owner reacted 👍 to: "<your question>"]`) and let the assistant act on the affirmative in context. No brittle
    question-detector in the daemon. Stays act-low **only while the resulting action is itself act-low**;
    if the "yes" would trigger something outbound/destructive, the normal ask-high gate still applies.
- **Phase C (approve) — ASK-HIGH, deferred:** a reaction as a *draft approval* (👍 on a held Slack/email
  draft = `send`). Outbound consequence → stays behind the existing approval-id flow; **do not** ship a
  reaction-approves-send path without an explicit go. Noted here only so it's scoped, not built.

### 3.4b As built — Phase A (observe)
- `telegram_poll.py`: `allowed_updates` now `["message", "message_reaction"]`; `extract_reaction` emits
  `{kind: "reaction", chat_id, message_id, emoji, from, …}` computing the **added** reaction as
  new-minus-old (Telegram sends whole arrays, not a delta; the diff runs over both plain and custom-emoji
  types via `_reaction_keys`, so switching between them still counts as a change). A removal, an unchanged
  set, or a chat outside the allowlist all yield nothing. Reactions ride in `messages` alongside normal
  items, which now carry an explicit `kind: "message"`. A Premium custom-emoji reaction is **not** skipped
  any more — see §3.6.
- **The reacted-to message had to be solved first.** Telegram's reaction update carries a `message_id`
  and *no message*, so a 👍 arrives context-free — there was nothing to quote and nothing to ack against.
  New `state/telegram-message-map.json` (`sentinel.record_sent_message` / `load_message_map`) records what
  the assistant sent, by message id: nudges from `check_reminders` (with the ⏰ `reminder_id`, which is what makes
  §3.4B possible at all) and chat replies from `deliver_reply` (which is what makes "👍 a question = yes"
  possible). Bounded to 200 newest, mutex'd (the reminder tick and the chat drainer both write it from
  worker threads in one process), and best-effort — it never raises into a send path.
- `presence.py`: `load_reaction_intents` (config `state/telegram-reactions.json`, fail-open to
  `DEFAULT_REACTION_INTENTS`, variation-selector-insensitive so ❤ = ❤️) + `_reaction_line` →
  `[the owner reacted 👍 (= ack) to: "<quoted>"]`, degrading to `to an earlier message` past the map's window.
  `reaction_context()` is loaded once per batch, and only when a reaction is actually in it.

> ⚠️ **Found while building, then verified: Telegram restricts which emoji can be used as reactions** to a
> server-defined allowed set (the Bot API's `ReactionTypeEmoji` list: 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉
> 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗
> 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷 😡 …). **⏰, 🤚 and ❔ are confirmed absent from it**, so
> three of the six mappings in Decision 2 can never fire. Resolved by the owner 2026-07-16: keep them, add the
> in-set aliases (😴/🥱 · 🤝/🙏 · ✍/🤔) — see the amendment in §3.3. Anything added to the map later must
> be in that set to be reactable. **Telegram Premium is the one way around this set-restriction**: a
> Premium reaction arrives as `custom_emoji` (an opaque id, not a plain emoji) — see §3.6 for how it's
> resolved back to a plain emoji so it can still hit the same map.

### 3.6 As built — Premium custom-emoji resolution

An owner with Telegram Premium can react with (almost) any emoji, not just the restricted set
above. Those arrive as `ReactionTypeCustomEmoji` — `{type: "custom_emoji", custom_emoji_id: "<opaque id>"}`,
no plain `emoji` field at all. Before this, `extract_reaction`'s emoji collector only understood
`ReactionTypeEmoji` and silently dropped the custom kind, so none of the owner's Premium-picker reactions
(⏰ 🤚 ❔ variants included) ever became a message — not even an unmapped `note`, just gone.

- `telegram_poll.py::_reaction_keys` normalizes both reaction types into one diffable key space
  (`e:<emoji>` / `c:<custom_emoji_id>`), so `extract_reaction`'s new-minus-old diff (§3.4b) works across
  either kind, or a switch between them. A custom-emoji reaction now always produces an item — with
  `emoji: ""` and a `custom_emoji_id` — rather than nothing.
- `resolve_reactions(messages, token, api_base, cache_path)` runs once per poll, after extraction, over
  the *whole* fetched batch: it collects every still-unresolved `custom_emoji_id`, resolves them in as few
  `getCustomEmojiStickers` calls as needed (the Bot API accepts up to 200 ids per call —
  `CUSTOM_EMOJI_BATCH`), and fills each reaction's `emoji` in place. One lookup per never-before-seen id;
  everything after that is a cache hit.
- **Cache:** `state/custom-emoji-cache.json` (gitignored like the rest of `state/`), a flat
  `{custom_emoji_id: base_emoji}` map, loaded/saved with the same tolerant, never-crash pattern as the
  rest of this file's state (`load_custom_emoji_cache` / `save_custom_emoji_cache`) — corrupt or missing
  ⇒ start empty, never raise.
- **Wiring:** the resolved emoji feeds the exact same `_norm_emoji` → `reaction_intent` path a plain
  emoji uses (`presence.py`), so a resolved Premium pick maps to an intent identically to a normal
  reaction.
- **Fail-open, all the way through.** No `TELEGRAM_BOT_TOKEN`, a network error, an id the API doesn't
  recognize, or a sticker with no `emoji` field of its own — every one of these just leaves that
  reaction's `emoji` blank, which downstream is indistinguishable from any other unmapped emoji: it
  degrades to the `note` intent (thread as context, no action) rather than raising or vanishing. A
  Premium reaction the assistant can't name is still a reaction it sees.

### 3.4c As built — Phase B (nudge-ack)
`presence.ackable_nudge()` is the whole safety story, and it is deliberately narrow. It returns a ⏰ row
id only when **all** of:
1. the reaction's intent maps to `ack` (the owner's config decides which emoji that is);
2. the reacted-to message is a **tracked nudge** with a `reminder_id` — a 👍 on a chat *reply* never
   auto-acks (that's the question-affirm case, which §3.4B puts at the warm-session tier by design);
3. that nudge was sent **today, local** (`reminders_acks.local_today` — the same owner-local-date rule
   the fire path gates on).

> **(3) is not incidental.** Reminder rows reset daily and an ack stamps *today's* date, so a 👍 on
> yesterday's nudge would mark **today** Done — for meds, precisely the failure that must not happen.
> Anything failing any clause is observe-only. The asymmetry is the point: not auto-acking costs the
> owner a few taps; auto-acking wrongly costs them a dose.

`ack_reminder_by_reaction()` then runs **the same calls the chat ack path makes** — deliberately, so
the two can't drift: `reminders_dequeue.py --reminder-id <row>` (cancels the obsolete re-nudges + records
the durable local ack) and — **on the notion backend only** (`store_backend_active()`; the write-behind
outbox is Notion-only, per store/notion/mapping.md's Outbox section) — `outbox.py ack --reminder-id <row>`
(journals the store `done` write for the next LLM turn to flush). Filesystem backends skip the outbox leg:
the local ack ledger still gates re-fires, and the store row itself is left to the warm session's
`store-update`. All idempotent, so a warm session that acks on top of it is harmless. All run in a
worker thread; a failure logs and the line goes through unacked rather than taking the daemon down.

When it fires, the threaded line **says so** (*"— I've already run the ack for you … no need to repeat
it"*), so the assistant acknowledges the owner rather than re-acking.

**The threaded line must say exactly how far the automated half got, and no further.** Neither call
above writes the store row itself — the dequeue records the local ack and the outbox (Notion backend
only) *journals* the `done` write for a later flush. A line that claims *"row marked Done"* at that
moment is not merely cosmetic: it is what makes the warm session stand down, so the one turn holding
the row id — the cheapest place to do the write — is told not to, and the row can stay un-Done for
days while the journal grows with no owner. The corrected shape names what landed and what is still
owed — *"I've dropped the re-nudges and journalled the ack, but the row's store write has NOT happened
yet: please write it through now"* — with the backend's own drain as the backstop if the turn doesn't.
On a filesystem backend (no outbox) the line says the row write is owed to the warm session's
`store-update`. A test that the acked line **never claims the row is done** pins the distinction;
that assertion is anti-regression, not wording taste. *(In this repository `presence.py` still
carries the older wording until the daemon-wiring port lands.)*

**Still ask-high, unchanged:** nothing here can send. Phase C (a reaction approving an outbound draft)
remains unbuilt.

### 3.5 Tests
- `allowed_updates` includes `message_reaction`.
- extraction computes the *added* emoji from old/new arrays; ignores a reaction *removal*.
- nudge-map lookup: 👍 on a tracked nudge → ack path called with the right `reminder_id`; 👍 on an
  untracked message → observe-only.
- §3.6: a custom-emoji reaction resolves via a mocked `getCustomEmojiStickers` and maps to the right
  intent; a cache hit skips the API; several ids in one poll batch cost one call (batched to
  `CUSTOM_EMOJI_BATCH`); an API failure (or no token) fails open to the same `note` an unmapped plain
  emoji gets; a corrupt cache file is tolerated. See `test_telegram_reactions.py`.

---

## 4. Feature — Reply-context threading

### 4.1 Behavior
When an inbound message carries `reply_to_message`, the quoted message's text is threaded into the warm
session so the assistant knows what the owner is replying to without a restate.

### 4.2 Design
- `poll_telegram` extracts `reply_to = msg["reply_to_message"].get("text"/"caption")` (truncated to a
  sane length, e.g. 300 chars) alongside the normal text.
- `telegram_task` / the prompt builder prefixes the enqueued item, e.g.
  `(replying to: "<quoted>") <their text>`, or adds a thread line. Keep it inside the existing
  `telegram-thread.json` continuity (`append_thread`), which already tails the last 6 turns.

### 4.3 Notes & tests
- Quoted message may itself be an attachment/reaction → quote a short synthesized descriptor.
- Test: a reply carries the quoted text into the enqueued item; a normal (non-reply) message is
  unchanged.

### 4.4 As built (PR 2)
- `telegram_poll.py::extract_reply_to` — parent `text` → `caption` → a synthesized descriptor for a
  quoted file (`a document named notes.pdf`, deliberately unquoted since the prefix wraps it) →
  `an earlier message` for a contentless parent. Truncated at `REPLY_QUOTE_CHARS` (300). A malformed
  `reply_to_message` is ignored rather than raising.
- `presence.py::telegram_inbound_text` prefixes `(replying to: "<quoted>") <body>`; the body itself is
  `_attachment_or_text`, so a reply *and* an attachment compose. **A reply prefix never resurrects an
  otherwise-empty message** — an empty line is what `telegram_task` drops, and a bare prefix carries no
  content of its own.
- The prefix rides the existing `telegram-thread.json` continuity unchanged (it's just part of the line).

---

## 5. Companion — post-restart "backlog ack" (optional, recommended)

**Problem seen live:** after `reseneschald`, the daemon drains the inbound backlog **serially and
quietly**, so from the owner's side it looked like only one message got through — they re-forwarded all
five.

**Fix:** on the first poll after (re)start, if the fetched batch has **N ≥ 2** inbound messages, send one
quick line *before* processing — *"Back up — got your N messages, working through them now."* Then answer
them in order. One extra send, only on a burst, only right after a cold start.

- **Detection:** a `state.just_started` flag set at daemon boot, cleared after the first non-empty poll.
- **Guardrail:** never fire on the steady-state single-message path (no chatter in normal use).
- **Autonomy:** act-low (an informational send to the owner's own chat).

### 5.1 As built (PR 4)
- `presence.py::_maybe_backlog_ack`, called from `telegram_task` **before** the enqueue, so the line lands
  ahead of the answers rather than trailing them. `DaemonState.just_started` starts `True` and is burned
  by the first **non-empty** poll (an empty poll is the norm and must not spend it) — ack or no ack, so a
  burst later in the run is silent.
- Sends via `deliver_reply`, which honors `--stub-send`: the offline suite can't message the owner for real.
- Best-effort: a failed ack logs and returns without threading — it costs us the ack, never the backlog
  it was announcing.
- **Scope note:** N counts the *fetched batch*, per the spec. Messages restored into the durable queue
  from a prior run (`presence-state.json`) don't count toward it. That case is rarer (a graceful
  `seneschald-update` restart only fires when the warm session is idle, so the queue is normally empty) and
  has its own poison-pill/dead-letter surfacing; folding it in would risk an ack after an ordinary
  reload. Revisit if a quiet restart-with-queued-backlog is ever actually observed.

---

## 6a. Feature — Message edits

**The defect.** `allowed_updates` named `["message", "message_reaction"]`, and naming any subset is an
implicit *"and nothing else"* — so `edited_message` was never delivered. Reactions were wired up
deliberately (§3.2); edits were simply never considered. The owner sends a fragment, edits it into
the full sentence a moment later, and the assistant only ever sees the fragment.

**Why it is worse than it sounds.** The failure is silent *and* asymmetric. On the owner's screen the
message reads as corrected; the assistant answers the uncorrected text; neither side can see the
divergence. A typo fix, a changed time, a retracted sentence — all invisible, and the resulting
confusion looks like the assistant misreading the owner rather than like a missing update type.

### 6a.1 The two cases, which are NOT the same thing

An `edited_message` carries the whole Message again — same `message_id`, the new text in full, never a
diff and never the text it replaced. What to do with it turns entirely on how far the original got:

| The original is… | Behavior | Why |
|---|---|---|
| **still un-answered** (in this poll batch, or sitting in the daemon's durable queue) | **replace the queued text in place** — one item in, one item out | The owner fixed a typo before the assistant got to it. The assistant should simply see the corrected message; enqueuing a second item would make one message read as two. |
| **already answered** | arrives as a new inbound: `[the owner edited an earlier message to: "…"]` | It cannot be un-answered. The assistant has to be able to react to the *correction*, and without the annotation the corrected text reads as the owner saying nearly the same thing twice. |

**The third case, decided explicitly: an edit that lands mid-turn is case 2.** The drainer takes
`pending[0]` into local variables and, on a delivered turn, pops index 0 *by position* — it never
re-reads the text. So rewriting the entry it is mid-turn on would answer the old text and then
discard the correction, silently: strictly worse than not handling edits at all. An in-flight marker
(set where the head is claimed, reset at the top of every drainer iteration) makes the replacement
refuse, and case 2 is the honest description of what is happening anyway — a turn is being spent on
the uncorrected text right now. The refusal is keyed on the **text**, not the index: a "session busy"
flag alone would not do, because the drainer awaits a session spawn between claiming the head and
setting that flag.

**A fourth outcome, silent by design:** an edit whose text is *unchanged* is absorbed as a no-op.
Telegram emits an `edited_message` for things the owner did not do — a link preview attaching is the
common one — and "the owner edited that to exactly what it already said" is a line that could only
ever be noise.

### 6a.2 As built

- **`telegram_poll.py`** (built): `allowed_updates` is now
  `["message", "edited_message", "message_reaction", "callback_query"]`; `message_payload()` picks the
  Message out of either key and reports its `kind`; an edit is extracted through the **same** path as
  a new message (text, caption, reply-to, media, allowlist) and marked `kind: "edit"` with
  `edit_date`. `message_id` is on **every** item, not just edits and reactions — the original has to
  be findable before its edit shows up. Malformed/partial payloads yield `(None, "")` and contribute
  nothing rather than raising through `main`'s un-caught loop.
- **Offset accounting is unchanged**, and that is load-bearing: an edit advances the cursor exactly
  like any other update, inside the same fetched-and-committed batch (§6). Tested explicitly across a
  mixed `message` / `edited_message` / `message_reaction` batch, because a regression here is a
  message-loss bug rather than a cosmetic one.
- **`presence.py`** (pending the daemon-wiring port): `apply_inbound_edit()` decides between the two
  cases; `replace_queued_inbound()` is the pure queue rewrite (mid-turn refusal, `attempts` carried
  over — an edit is not evidence the poison is gone); `_edit_line()` builds the annotation, in the
  same bracketed style as reactions and swipe-replies, **deliberately un-truncated** (unlike the
  reaction line's quote, the quoted text *is* the message here, so clipping it would drop the words
  the edit exists to deliver).
- **The id window.** The daemon maps `message_id` → *the line as it was enqueued*, bounded (200) like
  the outbound map, and recorded **after** the force-route transform — the queue holds post-transform
  text, so a key recorded before it would never match, and a replacement that skipped it would strip a
  `!fable` message's delegation directive on the way back in.
- **Not persisted, deliberately.** The queue survives a restart; this window doesn't, so an edit to a
  message queued before a reload is handled as case 2. Losing the window costs an annotation; a
  *wrong* match would cost the message, and that asymmetry is what decides it.
- **The continuity cache is amended too**, so a cold spawn's thread tail doesn't show the typo'd line
  and the corrected one as two separate things the owner said. Fail-open — it's a cache; the durable
  record (`turns.jsonl`) is written by the drainer at answer time and gets the corrected text with no
  help from here.
- **Media on an edit is re-fetched**, because an edit runs the identical extraction path: editing the
  *caption* on a photo re-downloads the photo. Named rather than special-cased — the duplicate costs
  one local copy of a file the owner already sent (the inbox is swept nightly), and a media-only
  branch here would be a second extraction path that could drift from the first, which is exactly
  what §1 warned about.
- **Tests:** `../scripts/test_telegram_edits.py` (the poller half here; the daemon half arrives with
  the wiring).

---

## 6b. Feature — The question picker

**The ask:** Telegram bots can carry buttons — can the assistant ask questions over Telegram the way
Claude Code's `AskUserQuestion` does, even if each question has to come as a separate message?

**Why it exists** is the standing picker rule, which *is* the requirement rather than context for it:
**a picker is one tap; a prose list is a writing assignment.** Reading several questions, holding
them all in working memory, and composing a reply that answers each in order is exactly the overhead
that makes a decision get deferred — and a half-answered question set is worse than an unasked one,
because work proceeds on the half that was answered.

Claude Code has `AskUserQuestion`, which renders selectable options. **Telegram had no equivalent, so
every decision the assistant needed over Telegram arrived as a prose list** — the exact friction the
rule exists to remove.

### 6b.1 The rule is the shape, not a comment

The rule has three clauses, and two of them are things a caller can simply forget. So
`telegram_ask.py` makes them structural:

| The clause | How it is enforced |
|---|---|
| *Give each option a real description of what it means and what it costs* | An option with no description is **refused** (exit 2, with the reason). There is no way to ask a bare-labels question through this CLI. |
| *Put the recommendation first and mark it `(Recommended)`* | The **first option IS the recommendation** and is marked automatically. `--no-recommendation` exists for a genuinely open pick and has to be typed. |
| *Use multi-select when the choices aren't mutually exclusive* | `--multi` — toggling checkbox buttons plus a Done row (§6b.3). |

**Where the descriptions live, and why the buttons don't carry them.** A Telegram inline-button label
is a phone-width string that truncates *silently*, so a real description cannot ride on the button —
it would be cut off exactly where the cost half of the sentence lives. So the **message body** carries
the numbered options with their full descriptions and the `(Recommended)` mark, and the **keyboard is
only the selector**, one numbered button per option, one per row. The number is what ties a truncated
button back to its description. This is the one place the design deviates from `AskUserQuestion`'s
look, and it is deliberate: the alternative is a picker that renders the rule unreadable.

**One question per message** — N questions = N messages. There is no paginated wizard here, and
adding one would be against the ask.

### 6b.2 Mechanism

- `sendMessage` with `reply_markup.inline_keyboard`.
- A tap arrives as a **`callback_query`** update, which must be **named in `allowed_updates`** — the
  same switch one update type further over (§6a). Without naming it, the keyboard renders and every
  tap on it is a no-op that spins forever.
- **`answerCallbackQuery` must be called or the client spins**, so it is called on **every** path,
  including the ones that cannot record an answer.
- `editMessageText` folds the choice back into the question's own message (`✓ <label>`, keyboard
  removed), so the question becomes its own record in the scrollback instead of leaving a dead
  keyboard behind. `editMessageReplyMarkup` does the same job for a multi-select toggle.
- **`callback_data` is capped at 64 bytes**, so it carries `q:<8-hex question id>:<index>` — an id and
  an index, **never the option text**. ~13 bytes; the cap is checked as a backstop.

### 6b.3 Multi-select

Built, not deferred: the durable store the feature needs anyway is what makes it cheap. A tap on an
option toggles it (`☑`/`☐` on the button) and `editMessageReplyMarkup` redraws the keyboard; **nothing
is delivered until Done.** Two consequences worth naming:

- **A toggle wakes nobody.** The assistant is not woken once per checkbox, and never reads a half-made
  selection as the final one — the half-answered-set rule at the level of a single question.
- **An accidental tap is free**, because it is undone by tapping again rather than by explaining.

Done with nothing selected is a real answer and is said plainly (`✓ (nothing selected)`), not treated
as a mistake.

### 6b.4 Durability, and the ordering that makes a tap safe

The pending question lives in **`state/telegram-questions.json`** (gitignored). It has to: the daemon
reloads on every merge — i.e. constantly — and a question must outlive that.

**The record is written BEFORE the send, with the `message_id` stamped in afterwards.**
`callback_data` carries the question id, so *the record* is what makes a tap resolvable; the
`message_id` only fuels the fold-the-answer-back edit. A crash between the two therefore costs the
message edit and never the answer. The reverse order would have made a lost write cost the answer
itself.

Within `resolve`, the same principle one level down: **decide → persist → answer the query → edit the
message.** A failed toast leaves the answer recorded and delivered; the reverse would leave the owner
told the tap landed with nothing on disk to show for it.

### 6b.5 A tap is never a silent no-op

The requirement, and the thing most of the branch count in this feature is spent on. Every tap the
daemon cannot honour still (a) answers the callback query so the button stops spinning, and (b) hands
the warm session a line so the assistant follows it up in words:

| What went wrong | The owner sees | The assistant receives |
|---|---|---|
| The question expired or was never known | An alert naming the question if a tombstone survives (§6b.6) | `[the owner tapped an answer button on a question I no longer have a record of ("…") — … ask them to say it in words]` |
| The payload is unreadable / not our button | An alert | `[the owner tapped a button whose payload I couldn't read — …]` |
| The option index is off the end of the question | An alert | `[the owner tapped an option I can't match to that question — …]` |
| The whole resolve failed — subprocess died, no token, unreadable store | **Nothing** (the API was never reached) | a fixed "callback unresolved" line — *the owner's app may still be showing it as pending; ask what they picked* |

The last row is the only one where the client keeps spinning, and it is precisely why the line names
that fact: the assistant can then say so rather than leaving the owner looking at a button that never
resolved.

Two **non**-failures deliver no line, and neither is silent from the owner's side because both produce
a popup: a multi-select toggle (nothing is decided yet) and a re-tap of a question already answered
(*"Already answered — <choice>"*).

### 6b.6 Expiry — 7 days, lazily, with a tombstone

A question nobody ever taps must not sit pending forever. **`QUESTION_TTL_DAYS = 7`**, and the defence
is the shape of a week rather than a round number: seven days spans a full weekday/weekend cycle, so a
question asked Monday can still be answered the following Sunday. Past a week the decision's context
has almost certainly moved, and answering it as if fresh is worse than asking the owner to restate.

Two details do the real work:

- **The sweep is lazy** — every load-modify-save of the store runs it. There is therefore no
  scheduled task and no Dream step to forget to wire up; a step nothing runs reads exactly like a step
  that ran, and this avoids the category rather than joining it.
- **An expired question leaves a tombstone** — its *text*, not its options, capped at `TOMBSTONE_CAP`
  (50). So a late tap can say **which** question expired instead of shrugging. A record that cannot be
  dated is retired too: an undateable question is one we can never prove is current, and the tombstone
  path is honest about that where keeping it forever silently would not be.

### 6b.7 As built

- **`telegram_ask.py`** (built, stdlib): `ask` / `resolve` / `list` / `prune` (plus the settle verbs
  `picker-state-marking-spec.md` adds). It reuses `telegram_send.py`'s env loader and API call rather
  than re-implementing them, so there is one HTTP path to the Bot API and one env loader. Both entry
  points take an injectable `api=` seam, so no test can reach the wire without saying so.
- **`telegram_poll.py`** (built): `callback_query` is in `allowed_updates`; `extract_callback()` emits
  `kind: "callback"` carrying `callback_id`, `data` and the question message's id. A query with **no
  `id`** is dropped — the id is the whole obligation, and with nothing to answer there is nothing to
  do. The allowlist gates it like every other kind, which deliberately leaves a *stranger's* button
  spinning rather than talking back to them (our keyboards only ever go into an allowlisted chat).
- **`presence.py`** (pending the daemon-wiring port): `resolve_callback()` is a thin subprocess
  wrapper in the shape of `sentinel.send_telegram` / `poll_telegram`, so a question's whole Bot API
  lifecycle lives in one module, and `_callback_line()` turns the result into inbound. Resolution runs
  in a thread — it makes up to three API calls — and a batch of pure taps skips loading the reaction
  context. **`--stub-send` refuses to resolve at all**: `answerCallbackQuery` is every bit as much a
  real send as a message, and a stub-brain run with a live `telegram.env` must not reach the owner.
- **The answer routes back as ordinary inbound**, `[the owner answered "…" → "…"]` — the same
  bracketed style as reactions and edits, so everything in brackets is still the daemon describing and
  never the owner speaking. The question is quoted short (`QUESTION_QUOTE_CHARS`); **the chosen labels
  never are**, for the same reason §6a leaves an edit's text un-truncated: they are the answer.
- **A tap contributes no id to the edit window.** A callback's `message_id` names one of the
  *assistant's* messages, so keying the edit window to it would aim a later edit lookup at the wrong
  entry. The same rule tightens the reaction case, which was latent-only (the owner cannot edit the
  assistant's messages, so no edit update could ever carry that id).
- **Offset accounting is unchanged** and tested across a mixed `message`/`edited_message`/
  `callback_query` batch — a regression there is a message-loss bug, not a cosmetic one.
- **Tests:** `../scripts/test_telegram_questions.py`.

### 6b.8 Deliberately NOT built — `setMyCommands`

Slash-command autocomplete was offered as a secondary *"only if it lands cleanly."* It doesn't, and the
reason is a question rather than an effort estimate: **the bot's existing directives use a `!` prefix**
(`!status`, `!fable`, `!private`) and `setMyCommands` registers `/`-prefixed ones. Whether slash
commands are a **parallel surface** (two vocabularies for one bot, and `!status` vs `/status`
diverging the first time one gains a flag) or a **rename** (every reference in `presence.py`,
`../modes/chat.md` and the owner's own muscle memory) is the owner's call, not an implementation
detail. It is left out entirely rather than guessed at. (`telegram-capability-map.md` §2.3 corrects
one premise here: registration does not gate delivery, so the real choice is whether the Menu Button
offers anything at all.)

---

## 6c. Feature — Albums: N updates, ONE turn

### 6c.1 The defect, and why it is plumbing

**Telegram has no "album" update.** Nine screenshots sent from one tap of Send arrive as **nine
`message` updates**, each carrying one photo, all sharing one `Message.media_group_id` (Bot API 3.5),
with the caption on exactly one of them. Enqueuing one entry per update, with the drainer answering
`pending[0]` **one entry per turn**, turns one message from the owner into up to nine turns, each one
an answer to a slice.

The observed shape: nine screenshots of one conversation, answered after the 1st, again after the 4th
and again after the 9th — the first reply mischaracterising a situation the later images explained,
the second recommending something a later image showed had already been postponed. **Chronic, not an
incident.**

**It was already found and named** — `telegram-capability-map.md` §2.3's `media_group_id` entry
predicted exactly this, contradicting the reading rule "several attachments sent together are ONE
message." That is the point: **a reading rule is written down and cannot bind, because the plumbing
hands the model a slice and the model has no way to know a slice is what it has.** The fix is a turn
boundary, not a sentence.

### 6c.2 The split: the poller carries, the daemon decides

`telegram_poll.py` extracts `media_group_id` onto the normalized record (`extract_media_group_id`,
built) and **does nothing else with it** — no coalescing, no holding, no reordering. Its offset
contract is untouched, and the reason is the contract itself: coalescing means *waiting*, waiting
means a message exists only in memory for a moment, and **nothing may be acked to Telegram before it
has been read.**

The coalescing is `presence.py`'s, in the inbound task: `album_key` → `album_absorb` → `album_due` →
`album_inbound_text`, and one durable hold. One album becomes **one queue entry**, therefore one turn.
*(Pending the daemon-wiring port; until it lands, album members still arrive as separate entries.)*

### 6c.3 The hold policy — two bounds, one free close

| | | |
|---|---|---|
| **QUIET** | `ALBUM_QUIET_SEC` = **2 s** | hand a group over once no new member has arrived for this long |
| **CAP** | `ALBUM_MAX_HOLD_SEC` = **15 s** | …and never past this, measured from the **first** member and not resettable |
| **free close** | — | any ordinary `message` that is not one of its members ends the group **on the spot, at zero added latency** |

The free close is what keeps the common shape — nine photos, then a question — instant **and in the
right order**: the album is queued ahead of the question it is the context for. It rests on album
members being contiguous, which is `[INFERRED]` (it follows from `sendMediaGroup` being one call and
this being a 1:1 chat with one sender; the Bot API does not spell it out). If that is ever wrong the
cost is one album split into two turns — the pre-album behaviour, never a loss. An **edit**, a
reaction and a tap close nothing: an edit lands on some *earlier* message and a reaction names one of
the *assistant's*, so closing on either could split an album mid-upload.

**The long poll shrinks to `ALBUM_POLL_TIMEOUT_SEC` (1 s) while anything is held.** Without it a 25 s
long poll parks the loop and the 2 s quiet window is a 25 s one. Nothing shrinks when nothing is held.

### 6c.4 The three rails

- **NEVER DELAY A MESSAGE THAT ISN'T IN AN ALBUM.** A record with no `media_group_id` — every plain
  text message, every lone photo — is dispatched in the cycle it was polled in. Its latency is
  byte-for-byte what it was. *A slower assistant is a worse assistant.*
- **NEVER HOLD FOREVER.** `album_due` runs on **every** cycle of the poll loop, including a cycle whose
  poll returned nothing and a cycle whose poll **failed**, so neither silence nor a transport blip can
  extend a hold. The clock is `time.monotonic`, so a wall-clock jump cannot either. A member cap (24;
  Telegram's own cap is 10) bounds the buffer, and the wind-down at the bottom of the loop flushes
  whatever is still held on every graceful stop — every restart and every merge reload.
- **NEVER LOSE A MESSAGE.** The poller has already committed the offset for these members and
  Telegram never re-sends an acked update, so the hold is **durable**:
  `state/telegram-album-hold.json`, written after every change. Everything uncertain **delivers** — an
  unreadable hold file, a non-string group id, a malformed hold record, the member cap: each resolves
  to "hand it over now", which is at worst the behaviour this replaces.

### 6c.5 What a restart mid-album does

**It delivers what it has, immediately, as one turn. It does not resume the hold.** The successor
cannot know how much of the album it has, and the offset says the rest may never come again. Members
that arrive *after* the restart form their own group and their own turn — exactly the split the
pre-restart daemon would have produced, and never a loss. The monotonic stamps are deliberately not
persisted (they are meaningless to another process), and the file is removed **before** the members
are handed back, so a crash in the dispatch that follows cannot re-deliver them — a duplicate would be
a message the owner never sent.

### 6c.6 The line, and one thing it deliberately gives up

`album_inbound_text` emits a header naming the count, then one descriptor per member in send order,
then the caption **once**. **The caption is searched for, not assumed to be on the first member** —
Telegram puts it on exactly one, and which one is the sending client's business, not a documented
guarantee. Each descriptor goes through the same attachment-or-text builder a lone photo already goes
through, so there is no second extraction path to drift (the §1 argument the edit path also rests on)
and the re-fetch recovery line is not lost by being inside an album.

**An album contributes no id to the edit window (§6a).** An edit to its caption would otherwise
replace the entry — nine descriptors and all — with the one edited caption string, losing the images
from the turn. With no id the edit falls through to the annotation path and arrives as the correction
it is, beside an album entry that is still intact. Costs an annotation; the alternative costs the
photos.

### 6c.7 As built

- `telegram_poll.py` (built) — `extract_media_group_id`; `media_group_id` additive on the record.
- `presence.py` (pending the daemon-wiring port) — `album_key` / `album_topic` / `album_inbound_text`
  / `album_absorb` / `album_due` / `album_flush_all` / `save_album_hold` / `load_album_hold`; the
  daemon state's `album_hold`; an inbound-dispatch helper lifted out of `telegram_task` (singles
  unchanged) so an album unit and a single share one queueing path.
- Tests: `../scripts/test_telegram_albums.py` — the poller field and the offset invariant across an
  album batch here; the daemon-side cases (both bounds and the free close, the split-across-two-polls
  case, caption not on the first member, a lone photo and a plain message undelayed, interleaved
  non-album messages, a failed poll, the shrinking poll window, restart mid-album, the wind-down flush)
  arrive with the wiring.

---

## 6. Cross-cutting

- **`allowed_updates`** is `["message", "edited_message", "message_reaction", "callback_query"]` at
  the **one** `get_updates` site (`telegram_poll.py`; see the §1 correction) — `message_reaction`
  unlocks §3, `edited_message` unlocks §6a and `callback_query` unlocks §6b. **Naming a subset is an
  implicit "and nothing else"** — and the narrowing is *sticky* across later calls — which is the whole
  §6a defect and would have been §6b's too. (Add `message_reaction_count` only if we ever want
  anonymous-group tallies — not needed for a private chat.)
- **Offset / durability:** unchanged. All new update kinds ride the same fetched-and-committed batch;
  extraction happens *after* the batch is in hand, so nothing new can be fetched-then-lost. **§6c is
  the one feature that holds a message after the offset moved**, which is exactly why its hold is
  durable (`state/telegram-album-hold.json`) — the poller itself still coalesces nothing and still
  acks nothing it has not read.
- **Fail-open everywhere:** any new branch that errors falls back to the current text-only behavior and
  logs; a plain text message must never regress.
- **Security:** allowlist gates downloads; filenames sanitized; size-capped; inbox pruned nightly.
- **Config:** optional `state/telegram-reactions.json` (the intent map) and
  `state/telegram-message-map.json` (nudge→message id) — both gitignored, seeded from `.example`s.
  `state/custom-emoji-cache.json` (§3.6, `custom_emoji_id` → base emoji) is also gitignored, but has no
  `.example` seed — it's a pure regenerable network cache, no vocabulary to hand-edit.
  `state/telegram-questions.json` (§6b) is gitignored and likewise unseeded — it is **runtime state,
  not config**: nothing in it is hand-edited, and an empty file is the correct starting point.
  `state/telegram-album-hold.json` (§6c) is the same, and is additionally **transient by design**: it
  exists only while an album is mid-hold and is removed as soon as the group is handed over, so its
  normal state is *absent*.
- **Docs:** update `seneschal/scripts/TELEGRAM_SETUP.md`, `CLAUDE.md` (Telegram inbound description), and
  `state/README.md` (new `inbox/` + the two map files) **in the same PR** as the code (stale docs = bug).

---

## 7. PR phasing (each merge-on-green, independently shippable)

1. **PR 1 — Attachment intake (§2). ✅ BUILT 2026-07-16.** Highest value (unblocks "just send the
   assistant the file"); self-contained. Includes the §2.4 retention sweep. Tests: `scripts/test_telegram_poll.py`.
2. **PR 2 — Reply-context threading (§4). ✅ BUILT 2026-07-16.** Small, pure-context, zero-risk.
3. **PR 3 — Reactions, Phase A observe + Phase B nudge-ack (§3.2–3.4B). ✅ BUILT 2026-07-16.** Phase C
   explicitly excluded. Landed as two commits — Phase A (observe, zero-risk) then Phase B (the ack) —
   mirroring the spec's own staging. Tests: `scripts/test_telegram_reactions.py`.
4. **PR 4 — Backlog ack (§5). ✅ BUILT 2026-07-16.** Tiny; nice quality-of-life. (Built before PR 3 —
   it's independent of the reaction work and small.)
5. **PR 5 — Premium custom-emoji resolution (§3.6). ✅ BUILT 2026-07-17.** Closes the one gap PR 3 shipped
   with by design (§3.4b's "custom-emoji reaction … extraction skips" was correct at the time — the
   owner's Premium picks were being silently dropped). Self-contained: `resolve_reactions` runs once per poll,
   after extraction, and everything else in §3 is unchanged. Tests: `scripts/test_telegram_reactions.py`.
6. **PR 6 — Message edits (§6a).** The same class of gap PR 5 closed, one update type over: an
   `allowed_updates` omission made a whole kind of inbound impossible rather than merely unhandled,
   and nothing about it was visible from either side of the conversation. Poller half built; daemon
   half pending. Tests: `scripts/test_telegram_edits.py`.
7. **PR 7 — The question picker (§6b).** Sequenced after PR 6, which it builds on rather than beside:
   they change the same `allowed_updates` line, and this is the third update type on a pattern PRs 3
   and 6 already established (ask for the type, extract it, annotate it into the same durable queue).
   The new surface is outbound — a keyboard and a durable pending-question store — but the inbound half
   is deliberately unremarkable. `telegram_ask.py` and the poller half built; daemon half pending.
   Tests: `scripts/test_telegram_questions.py`. **`setMyCommands` deliberately excluded — §6b.8.**
8. **PR 8 — Albums, N updates → ONE turn (§6c).** The first item here that is not an `allowed_updates`
   omission: the update type always arrived, the *field that binds nine of them into one message* was
   never read. It is also the first to change the **turn boundary** rather than the content of a turn,
   which is why the hold is the whole design and the two bounds are stated in §6c.3 rather than left
   to a constant. Poller half built; daemon half pending. Tests: `scripts/test_telegram_albums.py`.

### Deviations from the draft, decided while building PR 1
- **The §2.4 "reply" is the assistant's, not the daemon's.** The draft had the daemon emit a canned
  *"that's >20 MB…"* string. But Q1 says the warm session responds in context, and a canned line would be
  the assistant speaking out of character. So the daemon **describes** (`[attachment: … NOT downloaded —
  over Telegram's ~20 MB limit; they'd need to drop it on the machine instead]`) and the assistant says it
  in its own voice. Same guarantee — it never fails silently — without the daemon composing prose.
- **The attachment descriptor scrubs the filename.** The sender picks `file_name` and the descriptor is
  read by an LLM, so it goes through the same sanitizer as the write path — a name can't close the
  bracket and pose as an instruction. Thin threat model (the allowlist means it's the owner's own chat),
  but free.
- **One extraction site, not two** — see the §1 correction.

---

## 8. Decisions (resolved 2026-07-16, owner decisions)

1. **Attachment scope — GENERAL, no per-type auto-act.** Telegram isn't the big-data path; the owner
   just wants the assistant able to *respond to attachments generally*. → §2.1: download + surface path/caption,
   warm session responds in context; **no daemon auto-run** of any tool on an inbound file.
2. **Reaction map — the owner's authoritative 6-emoji set (updated 2026-07-16, supersedes the placeholder
   defaults):** 👍 ack · ❤️ liked · 👎 reject/drop-draft · ⏰ snooze · 🤚 hold-≥1-day · ❔ elaborate. See §3.3.
   **Amended same day**, once building revealed ⏰/🤚/❔ are outside Telegram's allowed reaction set and
   can never fire: originals kept, in-set aliases added — 😴/🥱 snooze · 🤝/🙏 hold · ✍/🤔 elaborate.
3. **Reaction-acks — reminders AND "👍 a question = yes."** → §3.4 Phase B split into nudge-ack (daemon,
   act-low) + question-affirm (warm-session tier, act-low unless the implied action is ask-high).
4. **Backlog-ack (§5) — YES, build it.**

Phase C (reaction-approves-an-outbound-draft) remains the **only** ask-high / deferred item; unchanged.
