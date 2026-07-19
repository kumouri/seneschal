# Telegram inbound enhancements — spec

**Status:** §2 attachment intake (PR 1), §4 reply-context (PR 2), §3 reactions Phase A+B (PR 3), §5
backlog-ack (PR 4), and §3.6 custom-emoji resolution (PR 5) are all **built** — see §7. **Phase C (§3.4)
remains ask-high and unbuilt**, the only deferred item.
**Author:** the assistant, on the owner's ask (2026-07-16)
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

**General-purpose, no per-type special-casing** (owner ruling, 2026-07-16 — Q1 resolved). This is *not*
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
- `seneschal/SKILL.md` — Dream step 2 calls the inbox sweep.

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

  > **Amended 2026-07-16 (owner ruling, after the constraint below surfaced): in-set aliases added, originals
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
- **Phase B (ack + affirm):** two act-low cases (owner ruling, 2026-07-16 — Q3 resolved: *reminders **and**
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

## 6. Cross-cutting

- **`allowed_updates`** becomes `["message", "message_reaction"]` at the **one** `get_updates` site
  (`telegram_poll.py`; see the §1 correction) — the single change that unlocks §3. (Add
  `message_reaction_count` only if we ever want anonymous-group tallies — not needed for a private chat.)
- **Offset / durability:** unchanged. All new update kinds ride the same fetched-and-committed batch;
  extraction happens *after* the batch is in hand, so nothing new can be fetched-then-lost.
- **Fail-open everywhere:** any new branch that errors falls back to the current text-only behavior and
  logs; a plain text message must never regress.
- **Security:** allowlist gates downloads; filenames sanitized; size-capped; inbox pruned nightly.
- **Config:** optional `state/telegram-reactions.json` (the intent map) and
  `state/telegram-message-map.json` (nudge→message id) — both gitignored, seeded from `.example`s.
  `state/custom-emoji-cache.json` (§3.6, `custom_emoji_id` → base emoji) is also gitignored, but has no
  `.example` seed — it's a pure regenerable network cache, no vocabulary to hand-edit.
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

## 8. Decisions (resolved 2026-07-16, owner rulings)

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
