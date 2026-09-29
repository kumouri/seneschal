# The Telegram capability map — what the assistant's bot could do, and what it actually does

**Status:** `MEMO(survey of Bot API 10.3; items since built from it — §2.1 ① topics, §2.2 D1/D3/D4, §2.3 media_group_id and setMessageReaction — are marked inline, with the daemon-side halves that are still pending named at each)`
— **A SURVEY, and it stays one:** the ranking below is not a plan of record and nothing in it is
scheduled. As written it changed no behaviour and made no Bot API call. **Several items have since
been built from it**, and each is marked where it sits:

- **§2.1 ① topics in private chats** — `../scripts/telegram_topics.py`: a routing table
  (`pull-requests` for merge-approval pickers, `decisions` as every other picker's DEFAULT,
  `reminders` for nudges), runtime-minted purposes (`telegram_topics.py add`,
  `dynamic-topics-spec.md`), and `telegram_send.py --topic` / `telegram_ask.py --topic` as the
  routing doors. The daemon side — a per-topic conversation cache, replies going back to the thread
  they came from, and reminder nudges routed into their topic — is specified here and lands with the
  `presence.py`/`sentinel.py` wiring. **`is_name_implicit` auto-naming is still unbuilt** and is that
  item's remaining headline.
- **§2.3 `media_group_id`** — the poller carries it; the album hold that turns N updates into one turn
  is `telegram-inbound-spec.md` §6c.
- **§2.3 `setMessageReaction`** — `telegram_ask.mark`, the primitive under
  `picker-state-marking-spec.md`: a merge picker's state as one reaction on the picker instead of
  prose after the owner has already tapped. It is the first thing built out of the coverage table in
  §4, which had it filed **UNKNOWN-UNKNOWN — surveyed, never weighed** — and the survey's own reasoning
  carried the design: no admin right, no notification, no echo, one reaction per message.
- **§2.2 D1, D3, D4** — fixed in the Telegram modules; D2's reminder-path half waits on the daemon
  wiring.

Read against **Bot API 10.3 (24 August 2026)**, the full changelog, `bots/features`, `bots/faq`,
`bots/webapps`, `bots/webhooks`, `api/forum` and `api/config` — downloaded and read locally rather
than answered from memory, by several parallel lanes over a non-overlapping partition of the
changelog. Prompted by the discovery that **topics in private chats** (9.3, 2025-12-31) existed and
nobody here knew. **The owner was right that there had to be much more, and by a wide margin.**
· **Owner:** the assistant

> **The one number that frames this document: the Bot API documents 146 methods. The bridge called 9
> when this was written, 10 once `setMessageReaction` shipped.** Measured, not estimated — the method
> list is every heading in `bots/api` whose body opens *"Use this method"*; the call list is every Bot
> API method-name string under `seneschal/scripts/`. That is **under 7 %**. Of the **27 update
> types**, the bridge subscribes to **4**.
> The gap is not uniformly interesting — 18 of the unused methods manage sticker sets and 21 manage
> group membership, neither of which a single-user assistant will ever want. But four clusters inside
> it are aimed squarely at what the assistant actually is, and **they all shipped in the last eight
> months**: topics in private chats, Rich Messages, streaming replies, and Secretary Mode. Telegram
> spent 2026 building for AI assistants and this bridge predates all of it.

---

## 1. Secretary Mode — settled

**Secretary Mode is the Business Bots toggle. It is not the topics switch, and it is not related to
one.** `bots/features`, under the anchor `#secretary-bots`, is titled **"Business Bots"** and opens:

> *"Bots can enable **Secretary Mode**, allowing users to connect the bot to their account so it can
> process incoming messages and, where permitted, respond on their behalf."*

Its quick-start step 1 is *"Enable Secretary Mode for your bot in @BotFather."* Every following step
is the Business API: handle `BusinessConnection` updates, handle `business_message` /
`edited_business_message` / `deleted_business_messages`, check `rights` on the latest
`BusinessConnection`, and pass `business_connection_id` to the send methods. So that menu entry is the
front door to **a bot acting inside the owner's own one-to-one chats with other humans** — not to the
assistant's own DM with the owner.

**The corroboration is Telegram's own inconsistent naming.** The Bot API 10.0 changelog reads
*"Allowed **Business Bots** to manage user accounts without a Telegram Premium subscription"*; the
API reference renders the identical change as *"Allowed **Secretary Bots** to manage accounts of
users without a Telegram Premium subscription."* Same sentence, same release, two names. A third
name, **"Chat Access Mode"**, appears exactly once in `bots/features` (in the bot-to-bot section)
and is never defined — flagged here rather than resolved.

**It does not carry the "disallow users to create new threads" switch.** That switch is documented
in the Bot API 9.4 changelog (2026-02-09) as living somewhere else entirely:

> *"Allowed bots to prevent users from creating and deleting topics in private chats through a new
> setting in the **@BotFather Mini App**."*

**Where the real topics toggle is.** `bots/features` says only *"Private-chat topics need to be
enabled for the bot via @BotFather"*, naming no menu. Combined with the 9.4 line above, and with the
fact that **no topics entry appears in the classic `/mybots → Bot Settings` menu**, the conclusion is
that **both topic switches live in the @BotFather Mini App, not in the text menu.** `bots/features`
says of the Mini App: *"It provides equivalent functionalities with a more modern and streamlined
UI"* — but "equivalent" is not borne out. **Guest Mode and Bot Management Mode are documented as
Mini-App-only too.** `[INFERRED — the docs never picture either menu]`: the text menu is the legacy
surface and has stopped receiving new toggles; **anything added since roughly Bot API 10.0 is
Mini-App-only.** That is the most useful operational fact in this section, because it means **the
classic menu is not evidence that a capability is missing** — three of the capabilities ranked
highest below are invisible from that menu by design.

**Every entry in the classic menu, and its state for this bridge:**

| Menu entry | What it is | State |
|---|---|---|
| **Inline Mode** | `/setinline`; lets anyone type `@YourAssistantBot …` in any chat. *"inline functionality has to be enabled via @BotFather, or your bot will not receive inline `Update`s"* | **Unused, correctly** — §2.4. Nothing calls `answerInlineQuery`, and `inline_query` is not in `allowed_updates`. One inline *button* variant is worth having (§2.4). |
| **Secretary Mode** | Business Bots — above | **Unused. The read-only half is §2.1 ④; the reply half is §2.5 ①.** |
| **Allow Groups?** | Whether the bot may be added to groups | Irrelevant while the owner is the only user. Leave as-is. |
| **Group Privacy** | Privacy mode. *"Privacy mode is enabled by default for all bots"*; in a group a privacy-mode bot sees only commands aimed at it, replies to it, and service messages | **Inert.** *"All bots will also receive, regardless of privacy mode: … All messages from private chats."* The assistant lives in a 1:1 DM, so this setting has no effect. |
| **Group Admin Rights** / **Channel Admin Rights** | Default rights requested on being added (`setMyDefaultAdministratorRights`) | Unused; matters only if a supergroup ever exists. |
| **Payments** | Pairs a payment-provider token | **Unused and should stay so** — §2.5 ④. |
| **Domain** | `/setdomain`, pairs the bot with a website for the Telegram Login Widget and `LoginUrl` buttons | **Unused.** Would matter only if the cockpit were publicly exposed, which is deferred by decision (`cockpit-spec.md`). |
| **Menu Button** | The default `MenuButton` (`setChatMenuButton`) | **Unused, and empty** — it has nothing to show because `setMyCommands` has never been called. See §2.3. |
| **Configure Mini App** | Main Mini App, splash screen, previews | Unused — §2.4. A Mini App needs a public HTTPS origin. |
| **Paid Broadcast** | `allow_paid_broadcast`: *"Pass True to allow up to 1000 messages per second, ignoring broadcasting limits for a fee of 0.1 Telegram Stars per message"* | **Unused, and not even available.** The FAQ gates it: *"a bot must have at least 100,000 Stars on its balance and at least 100,000 monthly active users."* (The API reference says *10,000* Stars — **the two official pages disagree; flagged, not resolved.**) The assistant sends to one person. |

**Not in that menu at all, and each matters:** private-chat **Topics** (9.3/9.4), the
**users-may-create-topics** switch (9.4), **Bot-to-Bot Communication Mode** (10.0/10.3), **Guest
Mode** (10.0), **Bot Management Mode** (9.6). All Mini-App-side.

---

## 2. The ranked map

Every capability carries the Bot API version it landed in, because **the reason nobody here knew
about most of this is that it shipped after the training data**. Six of the eight tier-1 items
landed between 2025-12-31 and 2026-08-24.

### 2.1 Tier 1 — would change how the owner works

**① Topics in private chats — 9.3 (2025-12-31); bot-created in 9.4 (2026-02-09).**
`bots/features`: *"Bots and users can organize long-running private conversations into separate
topics … without opening multiple chats"* and, unprompted, *"Topics in Private Chats keep separate
projects or support cases organized — **improving on established web-based AI chat UIs**."* Many
threads, one chat, no supergroup, no migration — **the chat id does not change**, so
`TELEGRAM_CHAT_ID` and the poller allowlist keep working. Two fields carry it inbound
(`Message.message_thread_id`, `Message.is_topic_message`, both documented *"for supergroups **and
private chats**"*); 33 send methods carry it outbound; `getMe` reports whether it is on
(`has_topics_enabled`). **`ForumTopic.is_name_implicit` is what makes it effortless rather than
merely possible:** *"True, if the name of the topic wasn't specified explicitly by its creator and
**likely needs to be changed by the bot**."* The owner types, a thread appears, the assistant names
it. The owner never picks and never labels. `editForumTopic` and `deleteForumTopic` also work in
private chats — so a label can be *improved* once the thread's real subject is known, and a resolved
thread can be removed. `closeForumTopic`/`reopenForumTopic` genuinely do **not**, so the archive verb
is delete, which is destructive and lands ask-high.
**Change:** `message_thread_id` through `telegram_send.params()` and `telegram_ask.ask()`; surface it
in `telegram_poll.message_payload()`; a new `telegram_topics.py` plus a state file — **the Bot API
has no `getForumTopics`, so a bot cannot enumerate its own topics** and must remember them or lose
them. The real cost is `presence.py`'s single-file conversation cache becoming per-topic.
**Gated on a @BotFather Mini App toggle no PR can flip.**

> **AS BUILT — the Telegram layer.** `../scripts/telegram_topics.py` owns the purpose→thread mapping
> and three constraints: the toggle is host-side (`topics_enabled` is a cached `getMe` detect, and the
> module is invisible when topics are off — byte-identical payload, no extra send, one nudge per
> boot); there is no `getForumTopics` (so `state/telegram-topics.json` is the only record of a
> created topic's id, written atomically and never pruned); and **fail open on every path** — any
> failure answers `None`, meaning the main chat.
>
> **The first row was the merge-approval picker.** Several green PRs, each with an approval picker
> buried somewhere in a day's conversation, and a picker the owner cannot find is a merge that cannot
> happen. So the merge picker routes to `pull-requests`.
>
> **The second row moved the DEFAULT.** Every *other* picker landed in the main chat and was lost in
> conversation — the same failure, one class wider. So `decisions` exists and `telegram_ask`'s
> `--topic` **defaults to it**. **The default is the mechanism**: a flag every existing caller would
> have to be edited to pass routes only the pickers written after somebody read the module. An
> explicit `--topic` still wins (verified through the real parser), and `TOPIC_MAIN_CHAT` (`main`) is
> the explicit opt-out and is **deliberately not a row**, so it resolves through the same
> unknown-purpose branch a typo does and `--topic` needs no argparse `choices`.
>
> **The third row, `reminders`, is drawn on a different line.** The picker rows are drawn on *does it
> wait for an answer*; this one on **does it interrupt**. A nudge answers nothing and waits for
> nothing — it arrives on a schedule the owner did not pick the moment of, in the middle of whatever
> they were saying. **Telegram only**: Discord is out of scope by decision, deliberately without a
> flag or a stub, because a half-built second channel is where a fail-open ladder rots unobserved.
> **The seam was a subprocess boundary, not a policy**: `sentinel.send_telegram` reaches Telegram by
> RUNNING `telegram_send.py`, so it holds no token and cannot call `telegram_topics.thread_id` itself;
> `telegram_send.py --topic PURPOSE` is that resolution on the far side of the subprocess, with every
> fail-open rung re-asserted against it (topics off, an unreachable `getMe`, a pre-9.3 bot, a corrupt
> state file, a failed creation, a module that will not import, a stored id Telegram now refuses). **A
> dropped critical nudge is the worst outcome in this system**, so a topic may only ever change WHERE a
> nudge lands.
>
> **Critical and call-me nudges go into the topic too — by decision, against the first-draft
> default.** The draft argued that the pierce set is defined by what it defeats — a quiet window, the
> night curfew, a day-off hold, the live-session defer, each a place the owner is not looking — and
> that a topic is another such place. **The first half stands; the last step had the client
> backwards.** On the owner's client a message in a topic is visible in the topic AND in the main
> chat, while a main-chat message is visible in the main chat only — so the topic is the strictly
> WIDER audience. **This is an observation of the real client, not a Bot API guarantee** (there is no
> `getForumTopics` to ask), so the premise is recorded at the one constant that holds the decision,
> `telegram_topics.REMINDERS_PIERCING_TO_MAIN_CHAT` — the line to reconsider if a Telegram change
> ever breaks the both-places property.
>
> **The ack round-trip is thread-blind by construction.** A 👍 resolves through
> `telegram-message-map.json` keyed on `message_id`, and `MessageReactionUpdated` carries no
> `message_thread_id` at all; a tap resolves by the question id in its `callback_data`, and
> `extract_callback` reads no thread either; a typed reply arrives on the same chat id, through the
> same allowlist and the same offset.
>
> **The table is data, and runtime-extensible.** `TOPIC_NAMES` is read from
> the gitignored per-install `telegram-topics.json` in `../references/`, falling back to the shipped
> `../references/telegram-topics.example.json`, re-read on every `thread_id`/`create_topic` call, and
> overlaid with purposes minted at runtime by `telegram_topics.py add` — no PR and no reload for a new
> topic (`dynamic-topics-spec.md`). `main` is filtered out of whatever the table says.
>
> **THE DAEMON SIDE — specified, landing with the `presence.py`/`sentinel.py` wiring.** *Topics as
> conversations, not mailboxes:* the single-file conversation cache becomes one file per thread
> (`state/telegram-threads/<key>.json`, the key being digits for a topic and the literal `main` for
> everything else — **the main chat is a topic like any other**, so there is no main-chat branch to
> drift, and the only strings that can reach a path join are `str(int)` and `main`: traversal is
> unreachable by construction). The topic rides the durable queue across every merge reload and comes
> back out through `telegram_send.params()`, the one builder every chunk and fallback rung goes
> through — so a three-chunk reply cannot arrive split across two conversations. **The legacy cache is
> migrated, not orphaned** (once per boot, before anything appends), and **an unreadable cache fails
> open to EMPTY, never to another topic's history** — a cold spawn with no continuity asks what this is
> about, while a cold spawn holding the wrong thread's conversation answers a question nobody asked,
> confidently. *Nudges into their topic:* the reminder sender passes `--topic`, and each topic-routed
> nudge is appended to its thread's cache at fire time — otherwise a bare *"done"* typed in the
> reminders topic arrives grounded on nothing. Main-chat nudges are deliberately not recorded; that
> restores parity for the topic without changing what every existing conversation is grounded on.
>
> **STILL UNBUILT, and it is this item's headline: `ForumTopic.is_name_implicit`** — *the owner types,
> a thread appears, the assistant names it.* Also absent by design: **no delete and no archive path**
> (close/reopen don't work in private chats; delete is destructive), and no `editForumTopic` — the
> assistant creates today's topics, so they are named explicitly at creation.
>
> **The toggle stays the owner's and is un-flippable from here**, so everything above is inert until
> they turn it on: with topics off the payload is byte-identical to a pre-topics send, and the daemon
> says so once per boot rather than silently never working.

**② Rich Messages — 10.1 (2026-06-11), extended in 10.2 and 10.3.**
The largest capability nobody here knew existed. `InputRichMessage` accepts **`markdown` directly** —
*"Rich Markdown is compatible with GitHub Flavored Markdown where possible and can contain arbitrary
HTML"* — at **32,768 characters** instead of 4,096, with up to 500 blocks and 25 block types.
Verified verbatim in the syntax reference: **GFM tables with alignment**, footnotes (`[^id1]`),
LaTeX (`$x^2$`, `$$…$$`, ```` ```math ````), task lists (`- [ ]`), six heading levels, and
`<details open><summary>` — **a collapsible block whose summary is always visible**. The assistant
writes Markdown; `telegram_format.py` is ~600 lines converting it *down* to Telegram's closed
eight-tag set, where a table becomes a padded `<pre>` and, by that module's own admission, emphasis
inside a cell is lost. Rich Markdown renders all of it natively. Three things land at once: **tables
are free** (word budgets count prose only); `<details>` is precisely *a label carrying enough context
to recall the rest*, with the rest one **tap** away rather than one scroll; and `<tg-button>` /
`<tg-button-row align=…>` mean a rich body and a picker can be **one message**, each button carrying
a `style`.
**Change:** a third rung on `telegram_send.send_text()`'s existing degradation ladder — rich → HTML
→ plain — with the degrade-to-plain kept as the floor. Rich parsing is a *larger* rejection surface,
and the invariant that outranks everything in that module is that a message is never lost to a
formatting failure.

**③ Streaming replies — `sendMessageDraft`, 9.3; ungated for all bots in 9.5; Stop button in 10.3.**
*"Use this method to stream a partial message to a user while the message is being generated."* It
is a **30-second ephemeral preview** that must be followed by a real `sendMessage` to persist —
*"once the output is finalized, you must call sendMessage with the complete message"*. Empty text
renders a *"Thinking…"* placeholder. `can_stop` puts a Stop button on it and delivers a
`stopped_message_generation` update when tapped. Today the warm CLI session goes silent for the
whole turn — **`sendChatAction` is called zero times anywhere in the tree**, so there is not even a
typing indicator — and an unbounded silent wait is exactly where an attention-fragile thread gets
abandoned. `can_stop` is the sharper half: **a user-side interrupt on a running generation, which
this repo has no mechanism for at all.** It accepts `message_thread_id`, so it composes with ①.

**④ Secretary Mode, read-only — Business Bots, 7.2; Premium requirement removed in 10.0 (2026-05-08).**
`BusinessBotRights` is fourteen à-la-carte grants, and the two that matter are **separable**:
`can_read_messages` (*"the bot can mark incoming private messages as read"*) and `can_reply`.
Granting the first and withholding the second gives the assistant exactly the posture it already
holds on email and Slack — screen and summarise, never send — applied to the owner's own Telegram
DMs, **with no autonomy-gate change at all**. The owner picks which chats, per chat; can pause one;
can permanently cut one off; and sees a persistent action bar in every managed chat. Two constraints
worth knowing: only **one** business bot may be connected to an account at a time, and `can_reply` is
scoped to *"private chats that had incoming messages in the last 24 hours"* — a bot cannot resurrect a
dormant conversation. **`can_reply` itself is category 4; the argument is §2.5 ①.**

**⑤ `force_reply` on an inline keyboard — 10.3.**
*"Pass True if the reply interface must be shown to the user, as if they had manually selected the
bot's message and tapped 'Reply'."* Re-fetched live and confirmed. This closes the one real hole in
`telegram_ask.py`: **the "none of the above" path.** A picker offers 2–10 buttons and nothing else;
if none fit, the owner must leave the picker, compose a free message, and hope the assistant connects
it — the writing assignment reappearing at the exact moment the picker existed to abolish it. Every
degrade branch in `resolve()` ends in *"Tell me in a message?"* with nothing tying that message back
to the record. With `force_reply`, the buttons stay **and** the input field opens already replying to
the question, so the answer arrives carrying the picker's `message_id` — which `telegram_ask.py`
already stores on every record. **It works only if `extract_reply_to` stops discarding the parent id
(⑦).**

**⑥ The `date_time` entity — 9.5 (2026-03-01).**
A message can carry a Unix timestamp that the *client* renders. `format="r"` — *"Displays the time
relative to the current time"* — is re-computed **every time the owner looks at it**, so "in 20
minutes" never goes stale the way a string composed at send time does. `w`/`d`/`D`/`t`/`T` give
localised weekday, short/long date, short/long time. This lands on two standing needs at once:
absolute time anchors, given unprompted, and rendering in the owner's local zone — the timezone
conversion stops being the assistant's job and becomes the client's. Available in HTML
(`<tg-time unix="…" format="r">`), MarkdownV2 (`![…](tg://time?unix=…&format=r)`) and Rich Markdown.
**Write the fallback text so it stands alone**: a client that does not support the entity shows the
literal text you supplied.

**⑦ Reply parameters, both directions.**
Inbound, `extract_reply_to` keeps a truncated **text descriptor** of a swipe-reply and discards
`message_id` and any thread id. That field is a free, exact, zero-friction thread selector — one
swipe, never leaving the chat — and it is the prerequisite for ⑤ and the precise override for ①.
Outbound, **the assistant has never sent a reply at all**: `ReplyParameters` (7.0) offers `quote` (an
exact substring of the parent — *"The message will fail to send if the quote isn't found in the
original message"*), `quote_position`, and cross-chat `chat_id`. Every reply today is a bare new
message, which is the friction underneath *"name what each reply answers."* Note `ReplyParameters`
has **no** `message_thread_id` — the thread is set on the send method, the reply in the reply block;
two separate parameters.

**⑧ Button `style` — 9.4.**
*"Must be one of "danger" (red), "success" (green) or "primary" (blue)."* Every button the assistant
sends is the same grey. The approval picker is **Approve / Not now**, where one option deploys to the
live daemon and the other does nothing, and on a phone they are identical rectangles. That is a
mis-tap surface on an ask-high gate, removed for two characters of JSON. **`DisabledButton` (10.3)**
is its companion: after a tap the un-chosen options can stay visible but dead, so the picker keeps
showing what was *not* picked — context currently thrown away. (Both are client-rendered; §5.)

### 2.2 Four defects the survey found on the way

Not unused capabilities — things that were wrong. They outrank most of §2.3 on value.

**D1 — FIXED. Non-text, non-media inbound was silently discarded, and the offset committed anyway.**
`MEDIA_KINDS` was `("document", "photo", "voice", "audio", "video")`, so a **sticker, video note,
location, contact, venue, poll or dice** fell out of `extract_media`, rendered as the empty string,
and was dropped by the inbound task's empty-line filter. The offset was already advanced, and the docs
are explicit: *"An update is considered confirmed as soon as getUpdates is called with an offset
higher than its update_id."* **Telegram never sends it again, and nothing logged it.** An animation
survived only by accident (Bot API 4.0 aliases it to `document`); `video_note` has no such alias.
**Fix:** `sticker` and `video_note` both carry a real `file_id` and joined `MEDIA_KINDS` — they
download and describe like every other attachment. `location`/`venue`/`contact`/`poll`/`dice` carry
no `file_id`, so `telegram_poll.describe_unsupported()` synthesizes a short line naming the kind and
its content (*"[shared a location: …]"*, *"[rolled 🎲 — 4]"*), which rides the ordinary `text` field
so it can never render empty and be dropped.

**D2 — the reminder path re-fired an ambiguous send.** `telegram_send` marks a failure
`{"ambiguous": true}` precisely so callers do not retry, and the chat-reply path must honour it — but
the reminder sender tested only `res.get("ok")`, so an ambiguous failure left the row armed and the
next tick re-delivered it: a blind retry of a request Telegram may already have delivered, **on the
channel that carries critical reminders.** The fix lands on one send path and the other one gets
missed — the recurring shape. **Fix (daemon side, landing with the `sentinel.py` wiring):** stamp the
row `ambiguous_send_at` (skipped by the loop's due-check exactly like `fired_at`/`suppressed_at`/
`acked_at`) and report `reminder_send_ambiguous` instead of firing again, never blind.

**D3 — FIXED. A refused 429 was misclassified as ambiguous.** Above
`telegram_http.MAX_RETRY_AFTER` the error was raised with `phase=None`, which `classify_send_failure`
read as `SEND_AMBIGUOUS`. But a 429 is the server saying it *refused* the request — nothing was
delivered and a retry cannot duplicate. A long flood-wait therefore dropped a chat reply outright and,
through D2, duplicated a reminder. The raise site already knows the status is 429, so
`telegram_http.request` now tags every 429-exhaustion raise (ceiling exceeded, attempts spent, or the
wall-clock budget spent) with `PHASE_REFUSED`, and `classify_send_failure` reads it as
`SEND_REJECTED` (safe to resend) rather than falling through to the ambiguous default.

**D4 — FIXED. `telegram_poll.py`'s `allowed_updates` comment was factually wrong, and the wrong model
was the hazard.** The comment said `message_reaction`, `edited_message` and `callback_query` were
*"ALL off by default"*. The docs say, verbatim and re-fetched live: *"Specify an empty list to receive
all update types **except chat_member, message_reaction, and message_reaction_count** (default). **If
not specified, the previous setting will be used.**"* Only **three** types are default-off, and
`edited_message` and `callback_query` have never been among them. The real mechanism is
**stickiness** — an earlier call passing `["message"]` persisted that narrowing across every later
call. Behaviour was correct because all four are passed explicitly every time; **the model taught to
the next maintainer was not**, and the model is what decides whether someone thinks omitting the
parameter is safe. It is also why every business update is foreclosed by construction (④): an
explicit list is an exhaustive "and nothing else." The comment at `get_updates()` now says so.

### 2.3 Tier 2 — genuinely useful

- **`setMessageReaction` (7.0) — BUILT, the first thing this memo has moved out of §4's coverage
  table.** The bridge *read* reactions and never set one. A reaction is the cheapest possible signal
  on a message the owner is already looking at: no notification, no words, no new message in the
  scrollback. No admin right is required in a private chat, and *"The update isn't received for
  reactions set by bots"* — so it cannot echo back and loop.
  **What it was used for is not the "received, working on it" this entry predicted.** It puts a merge
  picker's **state** on the picker — 🔥 spendable now, 👀 not yet, 😴 put to bed — after approval taps
  were spent on pull requests that could not merge, with the warning arriving in prose **after** the
  tap. **Two of the properties listed above turned out to be the load-bearing ones.** *No
  notification* is what makes re-marking the whole queue after every merge free, and therefore what
  lets the state be **derived every pass rather than latched**; *one reaction per message for a bot*
  (§3) is what forced one state per picker instead of a rank **and** a state. **The allowed-emoji set
  is the constraint nobody would have predicted**: keycap numbers and 🛏 are not in `ReactionTypeEmoji`
  and come back `REACTION_INVALID`. Design, measurement and residuals: `picker-state-marking-spec.md`.
  The *"ping progress on long jobs"* use is still unbuilt.
- **`sendChatAction` (legacy, `message_thread_id` since 9.3).** Strictly weaker than ③ but about ten
  lines and no @BotFather change. Eleven action types; status clears after five seconds.
- **`link_preview_options` (7.0) — and `telegram_send.py` sends a parameter that no longer exists.**
  `telegram_send.py` sends `disable_web_page_preview`, which returns **zero hits** in the current API
  reference (confirmed against both the local copy and a live fetch); Bot API 7.0 *"replaced the
  parameter disable_web_page_preview with link_preview_options"*. `[INFERRED]` it is still honoured
  for back-compatibility — it has never visibly failed — but it is undocumented and unversioned on the
  channel that carries critical reminders, and `LinkPreviewOptions` also offers `url`,
  `prefer_small_media` and `show_above_text`. On the approval picker specifically, the bare PR URL
  renders a full preview card that pushes the buttons down the screen, on the one message where the
  buttons are the point.
- **`url` button on the approval picker (legacy).** The PR link is a body line the owner must find
  and tap; an `[Open PR ↗]` row above `Approve` removes the hunt and frees body budget under
  `QUESTION_CHARS_MAX`.
- **Expandable blockquote (7.4).** `telegram_format.py` emits plain `<blockquote>` only. The
  collapsed variant is the right shape for "here's the answer, evidence one tap away." A cheaper
  cousin of ②.
- **`copy_text` / `CopyTextButton` (7.11).** A `[Copy SHA]` button beats selecting twelve characters
  on a phone.
- **`pinChatMessage` (legacy).** *"In private chats … all non-service messages can be pinned"* — no
  rights needed. One pinned "today's one thing" is a fixed anchor the owner never has to scroll for.
- **`getWebhookInfo` (2.2) as a free health probe.** *"If the bot is using getUpdates, will return an
  object with the url field empty."* It answers "has a webhook been set behind our back?" — the
  failure that makes `getUpdates` return `ok:false` forever. Never called here. (Whether
  `pending_update_count` means anything in pure polling mode is unsettled — §6.)
- **`my_chat_member` (5.1).** *"For private chats, this update is received only when the bot is
  blocked or unblocked by the user."* Not subscribed. If the owner ever blocks the bot, every send
  fails and nothing distinguishes that from a transport fault.
- **`media_group_id` (3.5) — BUILT in the poller, and the second item taken off this list.** Telegram
  delivers a five-photo album as five separate updates with the caption on one, so the assistant saw
  five unrelated attachment lines, contradicting the reading rule *"several attachments sent together
  are ONE message."* **A written reading rule cannot bind against plumbing that hands the model a
  slice**, so the fix is a turn boundary, not a sentence: `telegram_poll.py` carries the id and decides
  nothing; the daemon's inbound path holds a growing group for a quiet window (2 s) under a hard cap
  (15 s) and hands it over as ONE queue entry, i.e. ONE turn, while a message with no
  `media_group_id` is not delayed at all. The policy, its two bounds and what a restart mid-album does:
  `telegram-inbound-spec.md` §6c (the daemon half lands with the `presence.py` wiring).
- **`setMyCommands` (4.7), on a corrected premise.** `telegram-inbound-spec.md` §6b.8 defers this
  because the bot's verbs use a `!` prefix and `setMyCommands` registers `/`-prefixed ones. But
  **registration does not gate delivery** — it populates a typing shortcut, and a selected command
  arrives as an ordinary text message `[INFERRED: the docs never state that unregistered commands
  are filtered, and describe the menu purely as a selection affordance]`. So the choice is not "two
  vocabularies" but "does the Menu Button offer anything at all, or stay empty." **Still the owner's
  call** — the spec is right about that — but it should be re-put on the corrected premise.
- **The Telegram test environment.** A wholly separate environment
  (`api.telegram.org/bot<token>/test/METHOD`, separate account, separate bot) — the sanctioned way to
  exercise the Telegram path without messaging the owner for real, which is a standing hazard.
  Caveat: *"Flood limits are not raised in the test environment, and may at times be stricter."*

### 2.4 Tier 3 — interesting but no

- **Mini Apps, the whole surface.** Genuinely large — `CloudStorage` (1,024 items/user),
  `DeviceStorage` (5 MB), `SecureStorage` (iOS Keychain / Android Keystore), `BiometricManager`,
  location, accelerometer/gyroscope/orientation, haptics, full-screen, home-screen install,
  `shareToStory`, ~45 events, and an `initData` HMAC-SHA-256 scheme plus an Ed25519 signature that
  lets a **third party** validate without the bot token. Every launch mode requires a public HTTPS
  URL. The cockpit binds `127.0.0.1` and its public exposure is **deferred by decision**
  (`cockpit-spec.md`), so a Mini App is not "wrap the cockpit" — it is "reverse the exposure decision
  first." Two further disqualifiers even then: attachment-menu integration is *"currently only
  available for major advertisers on the Telegram Ad Platform"*, and every 8.0+ method silently
  no-ops on an older client unless the app checks `isVersionAtLeast`.
- **Inline mode as a content surface.** Twenty result types, 50 results per query. The point of
  inline mode is reaching *other people's* chats; this is a 1:1 assistant chat. The one exception
  worth keeping in view is `switch_inline_query_current_chat` — a button that **drops a half-written
  message into the owner's input field** for them to finish, removing the blank page without
  committing them.
- **Supergroup forums instead of private-chat topics.** Same idea, plus `close`/`reopen` and the
  General-topic suite — but it needs a supergroup, a fresh chat id with no history, the bot as admin,
  and it caps the bot at **20 messages per minute across the whole group** versus roughly 60 in a
  private chat. Worse, it moves the conversation to a second place to look. Revisit only if the
  missing soft-archive verb turns out to matter.
- **Channel direct messages / monoforums (9.2).** One topic per *sender*. The need is one thread per
  *subject*, from one sender. Orthogonal axis, and it needs the owner to run a channel.
- **Polls and quizzes.** A large 9.6 expansion — multiple correct answers, revoting, shuffling,
  user-added options, media in options. For one user, `telegram_ask.py`'s picker is strictly better:
  instant, silent, no tally ceremony, and it already folds the answer back into the message.
- **`sendDice`.** *"A dice message in a private chat can only be deleted if it was sent **more than**
  24 hours ago"* — you cannot delete a fresh one. Permanent litter.
- **`message_effect_id`.** Confetti on a reminder is noise, and no method enumerates valid ids.
- **Checklists (9.1).** Native Telegram todo lists, individually addressable via
  `ReplyParameters.checklist_task_id` (9.2) — genuinely close to Reminders mode. But
  `sendChecklist` is *"on behalf of a connected business account"*: **gated behind Secretary Mode**,
  and the store is already the todo system of record.
- **Managed Bots (9.6) and Guest Mode (10.0).** A bot that mints and holds tokens for other bots;
  a bot answerable in chats it is not a member of. Both intriguing next to the Archon roster, both
  solving a problem `jobs.py` and the Archon adapters already solve. Noted, not pursued.
- **`LoginUrl` / Telegram Login.** Would give one-tap auth into the cockpit; blocked by the same
  exposure decision, and the cockpit already has real OIDC.
- **Ephemeral messages (10.2/10.3) in groups.** `replace_callback_query_message` — a tap that swaps
  the message for a private overlay — is the most interesting new callback capability in the API.
  It is scoped to groups and supergroups. Category 3 by scope, not by merit. In the DM it is
  category 4 (§2.5 ②).
- **`sendGift`, Stars, subscriptions, paid media, games, stickers, chat-member administration.**
  A monetisation and community stack for a bot with customers. This bot has one user and no
  customers.

### 2.5 Tier 4 — actively bad ideas

**① Granting `can_reply` on a business connection — the assistant sending as the owner, in the
owner's real DMs.** The one that must be argued rather than waved off, because it is the largest
single step-removal available anywhere in this survey and the 24-hour window is a real safety
property.

1. **The gate's load-bearing property is that the assistant is legible as the assistant** — always
   the owner's assistant, **never impersonating the owner**. Business replies go out as the owner.
   Whether the human on the other end sees any "via bot" marker is a **client rendering** question
   the docs do not settle (§6). Building an impersonation capability on an *unverified* assumption
   that the recipient sees a disclosure is exactly the assumption-stated-as-measurement failure.
2. **Draft-and-hold cannot be implemented here.** The gate works because a held draft sits somewhere
   the owner reviews before it moves. A business reply has no draft state — `sendMessage` with a
   `business_connection_id` **is** the send. So the flow becomes: the assistant composes → asks → the
   owner taps → the assistant sends. That is **more** steps than the owner typing the reply in a chat
   they are already looking at. **It fails the stated test: it adds a step rather than removing one.**
3. **The third party never consented.** Everyone in those DMs is talking to the owner. The email/Slack
   analogy breaks precisely here: there the assistant drafts and *the owner* sends, so a human is
   always the last actor before the wire.
4. **The picker — the mandated decision surface — is structurally broken on that surface.**
   `CallbackQuery` carries no `business_connection_id` (full read of the object). MTProto has a
   dedicated `updateBusinessBotCallbackQuery` with `connection_id`; the Bot API exposes no
   equivalent. **A tap on a picker sent through a business connection arrives with no way to know
   which connection to answer through.**

**Verdict: `can_read_messages` yes, `can_reply` no — not "not yet."** That is the shape of the
thing, not a maturity gap. The honest alternative already exists: `savePreparedInlineMessage`, where
the assistant composes and **the owner sends with one tap** — draft-and-hold expressed as UI, no
impersonation — though today it is consumable only from a Mini App.

**② Ephemeral messages in the owner's DM.** *"Ephemeral interactions allow a bot and an individual
member of a **group or supergroup** chat to communicate privately on the public timeline without
cluttering the chat for other members."* There are no other members. What it costs is
disqualifying: *"It is not guaranteed that the user will receive the message, especially if they are
offline"*; `message_id` is **0**; a reply window of **15 seconds**; and even the deletion event is
*"not guaranteed"*. This is the channel that carries critical reminders, and `telegram_send.py`
exists around the rule that a message is never silently lost. **Ephemeral is a message class designed
to be lost.**

**③ Chat-wide auto-delete, and `protect_content`.** `message_auto_delete_time` is *"The time after
which **all messages sent to the chat** will be automatically deleted"* — chat-wide, not per-message.
The DM is a de facto durable log: `telegram_ingest.py` archives it and state caches derive from it.
Setting a TTL on it destroys evidence with no error path. `protect_content` is worse in a quieter way
— it blocks **the owner** from saving their own assistant's output.

**④ `allow_paid_broadcast`, anywhere on any send path.** One user; 30 msg/s free is thirty times the
busiest conceivable minute. The only way the flag ever fires is a **runaway loop** — and it converts a
bug that Telegram's own rate limiter would have contained into a **billed, uncapped** one. A spend gate
with no upside. Treat the parameter as permanently absent.

**⑤ Using `deleteMessage` to retract something the assistant got wrong.** Once deletion is wired the
temptation is to make a mistake disappear. That is the maximally-minimizing correction, against
*"own mistakes plainly"*, and it is worse than useless because `telegram_ingest.py`'s archive and the
live chat then disagree with no marker. **The correct primitive is `editMessageText`** — which, per
§3, has no documented age limit while deletion expires at 48 hours. Deletion is for *the owner's*
cleanup, never for the assistant's face-saving.

**⑥ A Mini App to replace the picker.** *"Put the decision in a real UI"* fails the picker's own test:
today is notification → glance → tap; a Mini App is notification → tap → cold start → read → tap →
close. Strictly more friction for any decision small enough to be a picker — which is every decision
the assistant asks about, by construction (`MIN_OPTIONS, MAX_OPTIONS = 2, 10`). It also drags in the
exposure reversal, a second auth surface, and a fourth dependency world. **The cockpit already is the
rich UI, and it is right that it is on the LAN.**

**⑦ A paginated multi-question wizard.** Not an API capability, but the thing keyboards tempt you
into. `telegram_ask.py` already forecloses it: *one question per message; N questions = N messages.*

**⑧ Rich Messages as a substitute for the picker.** ② will make beautiful structured reports, and
the pull toward "send the analysis and let the owner decide" will be strong. The standing rule is the
opposite. Rich messages carry `<tg-button>` rows, so the correct composition is **rich body *and*
buttons** — and `telegram_ask.py`'s refusal to accept an option with no description (exit 2) must
survive intact. A rich message ending in "let me know what you think" is a regression dressed as an
upgrade.

---

## 3. The full surface

**What a bot can do to a message it already sent, and for how long** — the question worth answering
precisely, because the asymmetry is load-bearing:

| Action | Documented limit |
|---|---|
| `editMessageText` / `Caption` / `Media` / `ReplyMarkup` on the bot's own message | **None stated.** The only time qualifier in all four is scoped to *business* messages the bot did not send (48 h). Silence is not a guarantee — §6. |
| `deleteMessage` | **48 hours.** Plus: a dice message in a private chat can only be deleted *more than* 24 h after sending. |
| `deleteMessages` | Same 48 h; 1–100 ids, strictly increasing; missing ids skipped silently. |
| `setMessageReaction` | No limit stated. One reaction per message for a bot. |
| `pinChatMessage` | No limit stated; no rights needed in a private chat. |
| `sendMessageDraft` | **30 seconds**, and it is not a message — it never persists. |
| `editMessageLiveLocation` | Until `live_period` expires; `0x7FFFFFFF` means forever. |

**So a bot can rewrite its own message apparently forever, but can only delete it for 48 hours.** The
durable correction primitive is the edit — which is also the ethically correct one (§2.5 ⑤).

**Scheduled sending does not exist in the Bot API.** Settled: `schedule_date` returns zero hits
across every downloaded page and was re-verified against the live reference. The only forward-dated
field anywhere is `SuggestedPostParameters.send_date`, which is a *proposal* on a channel
direct-messages post requiring human approval. MTProto has scheduling; bots get no door to it. A bot
can only *observe* it — `Message.is_from_offline` is set for *"an away or a greeting business
message, or as a scheduled message"*. **Consequence: `sentinel.py`'s reminder queue is not a
workaround for a missing feature, it is the only mechanism that exists** — and it is the right
architecture anyway, because server-side scheduling could not consult `presence_rules.py`'s
defer-while-busy gates at fire time.

**Update types: 27 exist, 4 are subscribed.** Never subscribed: `channel_post`,
`edited_channel_post`, `business_connection`, `business_message`, `edited_business_message`,
`deleted_business_messages`, `guest_message`, `message_reaction_count`, `inline_query`,
`chosen_inline_result`, `shipping_query`, `pre_checkout_query`, `purchased_paid_media`, `poll`,
`poll_answer`, `my_chat_member`, `chat_member`, `chat_join_request`, `chat_boost`,
`removed_chat_boost`, `managed_bot`, `subscription`, `stopped_message_generation`. **Exactly one
matters today: `my_chat_member`** (the block/unblock signal). One more,
`stopped_message_generation`, becomes necessary with ③. Everything else is correctly absent. Note
*"At most one of the optional fields can be present in any given update"*, so `message_payload`'s
ordered first-match loop is safe by construction.

**Limits that bind here** (source given because several widely-repeated numbers are folklore):

| Limit | Value | Source | Respected? |
|---|---|---|---|
| Message text | 1–4096 chars *after entities parsing* | api | **Yes** — and the splitter cuts the **source before** HTML conversion, so no tag straddles a boundary |
| Rich message text | **32,768** chars, 500 blocks, 16 nesting levels, 50 media, 20 table columns | api | n/a — unused |
| `callback_data` | 1–64 **bytes** | api | **Yes** — backstop raises; actual payload ≈13 B |
| `answerCallbackQuery` text | 0–200 chars | api | **Yes** |
| Inline keyboard button count | **not documented** — the widely-cited "100 buttons / 8 per row" is **folklore** | — | Self-limits to 2–10 as a product choice |
| Per-chat send rate | *"avoid sending more than one message per second"* — explicitly guidance, not a guarantee | faq | **No explicit pacing.** A 3-chunk reply fires back-to-back well inside 1 s |
| Per-group send rate | 20 messages/minute | faq | n/a — strongest argument against the supergroup route |
| `getUpdates` retention | *"they will not be kept longer than 24 hours"* | api | Yes, and documented in the module |
| `getFile` download | ≤ **20 MB**; link valid ≥ 1 hour | api | **Yes** — checked pre-flight; `file_path` never cached |
| `file_id` persistence | *"Yes, file_ids can be treated as persistent"* — but unique per bot, and one file may have several valid ids | faq/api | Yes — `file_unique_id` correctly used only as a filename fallback |
| Topic name | 1–128 chars | api | n/a |
| `getCustomEmojiStickers` batch | repo asserts 200; **no such number found in the docs** | — | **Folklore until tested at the boundary** |
| Entity count per message | **not documented anywhere**; "100 entities" is folklore | — | No cap needed |

**Local Bot API server** would lift the 20 MB ceiling (unlimited download, 2000 MB upload, absolute
`file_path`, any webhook port). The cost is disqualifying: `logOut` locks the bot out of the cloud
server for 10 minutes with **no documented way back**, and it means running and keeping alive a C++
server on the same box as the daemon. For "occasionally a file over 20 MB", the present behaviour —
describe it and ask the owner to drop it on the machine — is the better trade.

**Webhooks would require public exposure** — Telegram POSTs from fixed subnets to ports 443/80/88/8443
over TLS 1.2+, no IPv6, no redirects; a tailnet address is unreachable. But even granting the tunnel,
webhooks trade away the two properties this daemon is built on. **You lose replayability**: with
`getUpdates` an update stays on the server until *you* advance the offset; a webhook is judged
delivered by your HTTP status, so a 200 with a crash behind it is a permanent loss with no offset to
rewind — and `telegram_poll.py`'s whole correctness argument is *"the offset advances last."* **And
you lose "it just catches up" across reloads** — the daemon reloads on every merge, and a receiver
returning 410 for 23 hours can be *silently unsubscribed*. What you would gain is latency the daemon
already has.

---

## 4. What is unused, and whether that is deliberate

136 of 146 methods are uncalled (137 when this was written; `setMessageReaction` left the list).
Clustered, with the honest verdict on each cluster:

| Cluster | Count | Verdict |
|---|---|---|
| Sticker sets | 18 | **Deliberate.** No use for a single-user assistant. |
| Group / membership administration | 21 | **Deliberate.** No groups. Includes `setChatMemberTag` (9.5) — tags exist in the Bot API but are per-member in a supergroup, **not chat folders**; folders are MTProto/client-only. |
| Forum topics | 12 | **UNKNOWN-UNKNOWN — §2.1 ①.** The private-chat subset (`create`/`edit`/`delete`/`unpinAll`) is the highest-value cluster in the table; `createForumTopic` is now called. |
| Send-media (photo/video/audio/…) | 17 | **Deliberate** for outbound media — except `sendDocument`/`sendPhoto`, since falsified and built narrowly (`telegram-send-document-spec.md`); `sendChatAction` inside it is an **unknown-unknown** (§2.3). |
| Edit / copy / forward / pin / delete | 11 | **Mixed.** `pinChatMessage` and `deleteMessage` are unknown-unknowns; copy/forward were deliberate (one chat) until topics made them a mirroring candidate (`topic-mirroring-spec.md`). |
| Bot self-configuration | 15 | **Mixed.** `setMyCommands` + `setChatMenuButton` are a real gap (§2.3); the rest are cosmetic and BotFather covers them. |
| Rich messages + drafts | 3 | **UNKNOWN-UNKNOWN — §2.1 ②③.** |
| Ephemeral | 5 | **Deliberate — never**, §2.5 ②. |
| Business / checklists | 3 | **Split**: read-only is §2.1 ④; the write half is §2.5 ①. |
| Managed bots | 4 | Deliberate — `jobs.py` already owns delegation. |
| Payments / Stars / games / suggested posts | 8 | **Deliberate.** Nothing non-commercial in it. |
| Inline / web-app / guest answering | 3 | Deliberate — §2.4. |
| Webhook + session (`setWebhook`, `logOut`, `close`) | 5 | **Deliberate**, and argued in §3. |
| Reactions (outbound) | 2 | **`setMessageReaction` BUILT** — this table's UNKNOWN-UNKNOWN, and the first row that moved (§2.3; `picker-state-marking-spec.md`). The delete-reaction pair stays group-only. |
| Other | 5 | `getUserPersonalChatMessages` (reads a user's profile-pinned channel) and subscription invite links — deliberate. |

The pattern worth naming: **almost everything genuinely missing is missing because it shipped after
this bridge was written, not because anyone weighed it and declined.** The deliberate refusals —
plain-text-over-MarkdownV2, no reply keyboards, one button per row, one question per message,
long-polling over webhooks — are all documented in the modules themselves and all still correct.

---

## 5. Documented behaviour vs client behaviour

Telegram writes much of its UI contract as guidance to *client apps*. **A server does not enforce
any of these; a call still succeeds when a client ignores them.** These are the claims that break.

| Claim | Wording | What breaks if a client ignores it |
|---|---|---|
| Clients block topic-less sends when the bot manages topics | *"the API **always allows** users to send topic-less messages … graphical clients **should prevent**"* | **A topic-less inbound must be handled as a normal case, forever** — a missing `message_thread_id` means General, not "unknown". |
| The "Type any message to create a new thread" bubble | *"**should** always show"* | §2.1 ①'s zero-friction thread creation simply does not appear. |
| `is_name_implicit` means the bot should rename | *"**likely** needs to be changed by the bot"* | Advisory only; nothing rejects an unnamed topic. |
| Topic deletion is inferred from deleted message ids | *"clients and bots **should treat** the entire topic as deleted"* | **There are no message-deletion updates for private chats at all.** A bot cannot learn that the owner deleted a topic; the registry will hold stale entries and a send will fail at the wire. |
| Button `style` colours | *"If omitted, then an **app-specific style** is used"*; rich buttons: *"Apps **may** use theme-specific colors"* | `danger` is not guaranteed red — §2.1 ⑧ is a mis-tap *mitigation*, not a guarantee. |
| `force_reply` / `ForceReply` open a reply box | *"Telegram clients **will display** a reply interface"* | §2.1 ⑤ degrades to an ordinary picker. |
| `answerCallbackQuery` progress bar | *"Telegram clients **will display** a progress bar"* | Already handled correctly — the spin-forever failure is a client artifact. |
| `sendChatAction` clears on message arrival | *"Telegram clients clear its typing status"* | Cosmetic. |
| `date_time` fallback | *"the user **can still** receive the underlying date in their local format"* | **Write the literal text so it stands alone.** |
| Mini App version gating | `isVersionAtLeast` — every 8.0+ method silently no-ops on older clients | The app's job, not the server's. |

**Explicit non-guarantees — the server admits it may not deliver:** ephemeral messages *"not
guaranteed … especially if the user is offline"* (and their deletion events likewise);
`message_reaction_count` *"grouped and can be sent with delay up to a few minutes"*; and
**`Message.message_id` can be `0`** — *"the server might automatically schedule a message instead of
sending it immediately … the relevant message will be unusable until it is actually sent."*
`telegram_send.py` filters only `None`, not `0`, so a zero would be recorded as a real id. Latent
rather than live — the documented trigger is video to a large chat, which this bridge never does —
but it is the shape of thing that surfaces once outbound media exists.

**A documentation inconsistency worth recording.** The `message_reaction` update is documented as
*"The bot must be an administrator in the chat"* — yet reactions demonstrably work in a private DM,
where no administrator exists. `[INFERRED]` the admin clause is group-scoped phrasing never qualified
for private chats; the `allowed_updates` half is the part that actually binds. Separately, three
business methods name rights (`can_change_name` / `can_change_username` / `can_change_bio`) that **do
not exist** on `BusinessBotRights`, where they are `can_edit_*`. Anyone writing a rights-preflight
against the method prose will check keys that are never present.

---

## 6. Open questions — what a live call would settle

1. **Is topic mode already on for this bot?** One `getMe` reads `has_topics_enabled` and
   `allows_users_to_create_topics`. `telegram_topics.topics_enabled` now reads the first; nothing reads
   the second. **If false, §2.1 ① is blocked on a @BotFather Mini App toggle no PR can perform** — the
   same class of host-side gate as `TELEGRAM_FORMAT=markdown`.
2. **Is Secretary Mode already on?** `getMe.can_connect_to_business`, same call.
3. **How does the owner's Telegram Desktop actually render a bot private-chat forum?** Whether opening
   the chat lands in General or in a topic list decides whether §2.1 ① reads as "one place" or as
   tabs. Only a live enable-and-look settles it, and it is reversible.
4. **Does the "clients allow sending to General" behaviour hold in the owner's builds?** Documented as
   client guidance (§5). **The single highest-value thing to test first** — if Desktop ignores it, the
   whole shape collapses back into tabs.
5. **Do `style` (9.4), `force_reply` and `DisabledButton` (10.3) render in the client versions the
   owner runs?** Clients lag, which is why `isVersionAtLeast` exists.
6. **Does the recipient of a business reply see a "via bot" marker?** MTProto sets
   `via_business_connection`; whether any client renders a visible disclosure is unstated.
   **Load-bearing for §2.5 ① — the argument there rests on it being *unsettled*, not on assuming the
   worst.**
7. **Does `CallbackQuery` carry an undocumented `business_connection_id`?** The docs say no; MTProto
   has a dedicated update that does. One live tap settles it.
8. **Does `disable_web_page_preview` still work?** Zero documented hits; migrate to
   `link_preview_options` regardless of the answer.
9. **Is a bot's own message really editable forever?** The docs are silent, and silence is not a
   guarantee. One edit against a week-old message settles it.
10. **Does `getWebhookInfo.pending_update_count` mean anything in pure polling mode?** Decides
    whether §2.3's health probe is real or a constant zero.
11. **What is the maximum `parameters.retry_after`?** Undocumented; `MAX_RETRY_AFTER = 60` is a guess
    against an unknown distribution (D3).
12. **Is there a cap on topics per private-chat forum?** Undocumented, and "one thread per train of
    thought" is exactly the pattern that would find it.
13. **Does enabling threaded mode cost anything?** `api/forum` notes threaded bots are *"subject to
    an additional fee for Telegram Star purchases"* under ToS §6.2.6, which is not in the downloaded
    corpus. `[INFERRED]` it is a fee on Star purchases *made through* the bot, of which this bot makes
    none — **verify before enabling**, because "turning on topics costs money" is the kind of surprise
    that gets a feature reverted.

**Cheapest next step, and it costs almost nothing:** open the @BotFather Mini App, look at whether
Topics and Secretary Mode are listed, and flip Topics on with *users-may-create-topics off*. That
answers 1–4 in about ninety seconds, is fully reversible, and touches no code.

**Verification note.** Every capability claim here is quoted from the official docs. Claims that
were surprising — `disable_web_page_preview`'s disappearance, the Rich Markdown syntax table, the
`allowed_updates` default set, `force_reply` on `InlineKeyboardMarkup`, `sendChecklist`'s
business-only scope, `setChatMemberTag` — were **re-fetched from `core.telegram.org` and confirmed
against the live page** before being written down. The method count (146), the call count, the
update-type counts (27 / 4) and the cluster sizes were **measured**, not estimated. Where something
is inferred it is marked `[INFERRED]` inline; where a claim rests on client behaviour it is in §5.

## Summary

Everything the Bot API can do that this bridge doesn't. **146 methods documented, about 7 % called;
27 update types, 4 subscribed.** Secretary Mode is the Business Bots toggle, not topics; the topic
switches are @BotFather Mini App-only. Tier 1, all shipped after the bridge was written: **topics in
private chats** (built in the Telegram layer; daemon-side per-topic conversation pending;
`is_name_implicit` auto-naming unbuilt), **Rich Messages**, **streaming replies**, **Secretary Mode
read-only**, **`force_reply` on an inline keyboard** (the picker's missing *none-of-the-above* path),
the **`date_time` entity**, reply parameters, and button `style`. **Four defects**: silently dropped
non-media inbound (fixed), the reminder path re-firing an ambiguous send (daemon-side fix pending), a
refused 429 misread as ambiguous (fixed), and a wrong `allowed_updates` comment — the real mechanism
is **stickiness** (fixed). Category 4, argued: **`can_reply` on a business connection is a no
forever** — it adds a step, it impersonates the owner, and `CallbackQuery` carries no
`business_connection_id`. Also never: ephemeral in the DM, chat-wide auto-delete,
`allow_paid_broadcast`, `deleteMessage` as a retraction, a Mini App instead of the picker.
