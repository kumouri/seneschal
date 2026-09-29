# Telegram setup — bot + two-way chat (one-time)

Telegram is the assistant's free, always-with-you surface: reminders, nudges, approval prompts,
tappable question pickers, and its side of an actual back-and-forth chat all flow through a Telegram
bot. It costs nothing and works from your phone. The scripts (`telegram_send.py` outbound,
`telegram_ask.py` question pickers, `telegram_poll.py` inbound) are already built — you just need to
create the bot and drop its token into `telegram.env`.

> **Prerequisites:** a Telegram account and the Telegram app. That's it — no paid plan, no server.

## Steps

1. **Create the bot.** In Telegram, message **@BotFather** → `/newbot`. Give it a name (e.g. your
   assistant's name) and a username ending in `bot` (e.g. `my_seneschal_bot`). BotFather replies with a
   **token** like `123456789:AA...`. Keep it secret — it's the bot's password.
2. **Start a chat with your new bot** and send it any message (e.g. "hi"). A bot can't message you until
   you've messaged it first.
3. **Create `telegram.env`** next to these scripts: `cp telegram.env.example telegram.env` and paste the
   token into `TELEGRAM_BOT_TOKEN`. `telegram.env` is git-ignored — never commit it.
4. **Verify the token** (no message sent):
   ```sh
   python telegram_send.py --check-auth --env-file telegram.env
   ```
   Should print `{"ok": true, "bot": "...", ...}`.
5. **Find your chat id.** With the message from step 2 waiting, run:
   ```sh
   python telegram_poll.py --env-file telegram.env
   ```
   Read `chat_id` from the output. Put it in `telegram.env` as both `TELEGRAM_CHAT_ID` (default recipient)
   and `TELEGRAM_ALLOWED_CHAT_IDS` (so only your chat can talk to the assistant).
6. **Round-trip test:**
   ```sh
   python telegram_send.py --text "Assistant here — wired up." --env-file telegram.env
   ```
   You should get the message on your phone. Then reply to the bot and run
   `python telegram_poll.py --commit --env-file telegram.env` — your reply comes back as JSON and the
   offset advances so it isn't re-read.
7. **Tell the assistant "Telegram is up."** It will start using it for reminders and replies; outbound
   that's ask-high (a real email/Slack send) still waits for your approval.


## How it's used

- **Outbound** (`telegram_send.py`): reminders, proactive nudges, "ready to send?" approval prompts, and
  the assistant's chat replies. Exit 0 = sent, 1 = send/transport failure, 2 = bad arguments, **3 =
  suppressed by a Watch gate** (below — nothing was sent, and nothing is wrong). `--photo` and
  `--document` send a single image or file (`sendPhoto` / `sendDocument`, 50 MB cap).
- **Approving a held draft over Telegram:** reply `send a7` (or `drop a7`) to the push that announced
  it. The message wakes the warm session in Chat mode, which **records the approval first** —
  `python pending_approvals.py resolve a7 --status approved` — and only then sends, because the
  outbound send gate (`send_gate.py`) refuses a non-owner recipient that has no `approved` row. Rule:
  `../references/autonomy-policy.md`; host-side gate setup: `SEND_GATE_SETUP.md`.
- **Question pickers** (`telegram_ask.py`): tappable buttons when the assistant needs a decision — see
  [Question pickers](#question-pickers--tappable-buttons) below.
- **The Watch gates** (optional): a send from a **Watch** surface (`SENESCHAL_SESSION_SOURCE=watch`, or
  `--ack-gate`) that *chases* something already handled — a ⏰ Reminders row acked today, a topic on the
  suppression list, a fact the owner said is handled — is refused rather than sent, because the headless
  comms peek is a second sender the reminder queue's own ack gate never covered. Pass
  `--reminder-id <⏰ row id>` to make the reminder check exact instead of title-matched; `--no-ack-gate`
  opts out. The gates live in the optional Watch gate modules; **when those aren't installed, nothing is
  gated** and the send goes out exactly as with `--no-ack-gate`. When they are, every verdict is logged
  to `../state/watch-gate.jsonl`, and anything the gate can't determine sends. Nothing changes for the
  warm session or the reminder fire path: neither runs on a watch surface. Why:
  `../references/reminders-policy.md` ("The second sender").
- **Inbound** (`telegram_poll.py`): the sentinel polls each cycle with `--commit`; new messages wake the
  full brain in **Chat mode** to reply (one persona across `/assistant`, Telegram, and scheduled runs).
- **Offset** lives in `../state/telegram-offset` (gitignored). Delete it to replay the last ~24h. It only
  ever advances **after** a poll that succeeded and was processed — Telegram never re-sends an update
  you've acknowledged, so a failed read has to leave it exactly where it was. Every poll cycle also
  appends one line (update ids and counts, never message text) to `../state/telegram-poll-trace.jsonl`,
  so "was the daemon even polling?" always has an answer.

## Transient network failures — what retries, and what deliberately doesn't

A flaky minute of network shouldn't cost whatever it lands on — one TCP reset (`[WinError 10054]`) in
the middle of a photo download would otherwise hand the assistant a *"download failed"* placeholder
instead of your picture. Every Telegram HTTP call goes through one shared transport (`telegram_http.py`)
with bounded exponential backoff, jitter, an attempt cap and a total time ceiling.

**The split is the point, and it is not symmetrical:**

| Call | Retries? |
|---|---|
| `getUpdates`, `getFile`, the attachment download, `getMe`, `getCustomEmojiStickers` | **Yes, freely** — resets, DNS failures, TLS errors, timeouts, 429s and 5xx |
| `sendMessage`, `sendPhoto`, `sendDocument`, `editMessageText`, `answerCallbackQuery` | **Only failures that provably happened before the request went out** — DNS, the TCP handshake, the TLS handshake, a connect-phase refusal or reset — plus HTTP 429 |

**Why sending is the cautious one.** Telegram's `sendMessage` has no idempotency key, so a connection
reset can land *after* Telegram accepted the message and *before* the reply came back. From this end
those two look identical, and retrying the second one sends you the same message twice — on the channel
that carries your critical reminders. So a failure that happened once the request was on the wire is
reported rather than retried, and the error says so in as many words. **Under-sending is recoverable;
double-sending is not.** HTTP 429 is the exception in both directions: it means Telegram *refused* the
request, so nothing was delivered and a retry can't duplicate anything — and it waits for the number
Telegram gives (`retry_after`) rather than a schedule of our own.

None of this is the **formatting** fallback below. That one re-sends a message Telegram *rejected*, as
plain text; this one is about the connection. They're separate mechanisms and neither covers the other —
but they answer the same question the same way: a fallback that re-sent on *every* failure, including
the ambiguous one this policy refuses to retry, would land the HTML and the plain copy both.

## Formatting — Markdown → Telegram HTML

**The assistant writes Markdown.** Until you switch this on, Telegram shows it verbatim: literal `**`
around every emphasised phrase, `#` in front of every heading, `|---|` rows under every table. Turning
it on converts that Markdown to Telegram's small HTML tag set at the send boundary.

**The switch is one line in `telegram.env`:**

```ini
TELEGRAM_FORMAT=markdown
```

and the rollback is the same line set back to `plain` (or deleted). No restart is needed for a
one-off `telegram_send.py` call; the resident daemon picks it up on its next reload.

`telegram.env` is gitignored — it exists only on your machine — so the converter ships as *machinery*,
not behaviour: until you set that line, every Telegram message is still plain. `telegram.env.example`
carries `TELEGRAM_FORMAT=` (empty), and an empty value normalizes to `plain`.
`TELEGRAM_PARSE_MODE` is unchanged and still means what it always meant — the literal `parse_mode`
handed to the Bot API. `TELEGRAM_FORMAT` is a **separate** knob that decides whether we convert
first. **Default is `plain`**, and an unrecognised value reads as `plain` rather than breaking the
channel.

See it before you send it — this touches no network:

```sh
python telegram_send.py --dry-run --format markdown --text "**bold** and \`code\`"
```

**A message is never lost to a formatting failure.** If Telegram **answers and rejects** the converted
HTML, that message is re-sent immediately — once, as plain text, with the original unconverted
string — and every fallback is logged to `../state/telegram-format-fallback.jsonl`. This is why
`MarkdownV2` was **not** the answer: there, one unescaped character is a 400 and the message is
simply gone, which is unacceptable on the channel that carries your critical reminders.

**And a message is never sent twice by that fallback either.** The two edges of *exactly once* are one
rule. If the HTML attempt fails **after the request went out** — a read timeout, a mid-flight reset —
Telegram may already have delivered it, so nothing is re-sent, nothing further is attempted, and the
send reports failure with `"ambiguous": true` in its JSON. **A caller that sees that flag must not
automatically send the text again.** The log row says `"resent": false`, which is how you tell a
stopped send from a working fallback:

```sh
grep '"resent": false' ../state/telegram-format-fallback.jsonl   # sends that STOPPED
```

A failure that provably never left the machine (DNS, the TCP/TLS handshake) still falls back normally —
nothing was delivered, so re-sending can't duplicate.

| Markdown | Telegram |
|---|---|
| `**bold**` | **bold** |
| `*italic*`, `_italic_` | *italic* |
| `~~strike~~` | ~~strike~~ |
| `` `code` ``, ```` ```blocks``` ```` | monospace (with a language tag if you give one) |
| `[text](url)` | a real link — `http`/`https`/`tg`/`mailto` only |
| `> quote` | a quote block |
| `# heading` | a **bold line** — nothing in Telegram renders a heading |
| a table | a monospace block with the columns padded so they line up |
| `- item`, `1. item` | left exactly as typed; lists are not marked up |

**What it deliberately will not do**, so none of it is a surprise later:

- **An `_` inside a word stays an `_`.** `telegram_send.py`, `state_dir` and `__init__` turn up in
  messages constantly and must survive. `__bold__` is unsupported for the same reason — it would turn
  `__init__.py` into a bold *init*.
- **A table becomes a monospace block, so nothing inside a cell can be a link or bold.** Emphasis in
  a cell is flattened to plain text and a link keeps its URL in parentheses but stops being tappable.
  A wide table scrolls sideways on a phone rather than wrapping.
- **Stray markers stay literal.** A lone `*`, an unclosed backtick, an unclosed fence: each degrades
  to readable text rather than breaking the message.
- **Long messages split at 4096 characters** — Telegram's cap, not ours. The *source* is cut before
  conversion, so a tag can never straddle the boundary and a long message is never plainer than a
  short one.

## The token never appears in an error message or a log

The Bot API puts your token in the **path** — `https://api.telegram.org/bot<TOKEN>/sendMessage` — so
an error message that quotes the URL quotes the credential. Error messages are persisted, into
`../state/telegram-format-fallback.jsonl` and the daemon's `../state/presence.log`; both files are
gitignored, but both are read by agents and by anything doing comms diagnostics, which is not a place
a live credential belongs.

Everything that formats a Bot API URL for a human or a log goes through one helper
(`telegram_http.redact_url`), and `TelegramHTTPError` scrubs its own message on the way in, so a
message built by some future route is covered too. What you see instead is:

```
https://api.telegram.org/bot<redacted>/sendMessage: The read operation timed out …
```

**The method name deliberately survives.** `sendMessage` failing and `editMessageText` failing are
different incidents, and an error you cannot diagnose is one somebody eventually un-redacts.

Logs written by an older, unredacted build may still carry the token. Clean them — it is idempotent
and changes nothing else in the row:

```sh
python scrub_telegram_token.py --scan     # which files carry it, and how many hits
python scrub_telegram_token.py            # clean them; prints how many rows changed
```

It uses a **different write mode per file, and that is not a preference.** The fallback log is
opened, appended to and closed per row, so a temp file + `os.replace` is safe there. `presence.log`
is opened **once** when the daemon starts and held for its whole life — renaming a new file onto that
path orphans the daemon's handle, and every log line after that goes somewhere nobody can see. So
that one is redacted **in place**, over the token, at the same offsets and the same width:

```
[2026-01-01T00:17:23-05:00] ! telegram poll: https://api.telegram.org/bot<redacted----------->/getUpdates: …
```

The dashes are padding. Nothing is renamed, nothing is truncated, the file's size is unchanged, and
the daemon keeps logging through it without noticing — **you do not need to stop the daemon to run
this.**

**This is not by itself a reason to rotate the token, and rotating is your call** — a rotation takes
the daemon's comms down until every `telegram.env` on the machine is updated.


## Reactions

React to one of the assistant's messages and it reads that as a lightweight intent — no typing needed.
The vocabulary is yours, in `../state/telegram-reactions.json` (copy the `.example`; with no file the
same defaults apply):

| Reaction | Intent | Means |
|---|---|---|
| 👍 | `ack` | yes / confirm / accept — on a reminder nudge, that's a Done |
| ❤ | `liked` | warmth about the reply itself; no action |
| 👎 | `reject` | no / drop a held draft / don't accept |
| 😴 · 🥱 · (⏰) | `snooze` | more time; bring it back later |
| 🤝 · 🙏 · (🤚) | `hold` | wait ≥ 1 day, don't resurface unless I ask |
| ✍ · 🤔 · (❔) | `elaborate` | explain / tell me more |

Anything else is a `note` — threaded to the assistant as context, no action. The intent is a **hint**:
the assistant acts on it in context, so a 👍 on a question reads as "yes" and a 👎 on a held draft drops
it. The one thing the daemon does on its own is ack a same-day reminder nudge; everything with an
outbound consequence still goes through the normal approval gate (a 👍 can't send an email).

> ⚠️ **The parenthesised ones don't work — Telegram's fault, not ours.** You can only react with emoji
> from Telegram's own allowed set (the Bot API's `ReactionTypeEmoji` list), and **⏰, 🤚 and ❔ are not in
> it** (verified against the list), so the picker will never offer them. They're kept in the map because
> they record what you meant, but the **in-set aliases are the ones that fire**: 😴/🥱 *snooze*, 🤝/🙏
> *hold*, ✍/🤔 *elaborate*. Use those. If you'd rather have different ones, anything from Telegram's set
> works — 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 🎉 🙏 👌 💯 😢 🤩 ⚡ ✍ 🤝 🫡 😴 🥱 🤗 😎 🗿 🆒 🦄 💊 … — edit
> `../state/telegram-reactions.json`; no restart needed (it's read per batch).

Reactions arrive only because the poller explicitly asks for `message_reaction` updates — they're off by
default in the Bot API. Spec: `../docs/telegram-inbound-spec.md` §3.

**Telegram Premium?** No setup needed. Premium lets you react with ~any emoji via `custom_emoji_id`
rather than a plain one, and the poller resolves those automatically via `getCustomEmojiStickers`,
caching each id → base emoji in `../state/custom-emoji-cache.json` (gitignored) so a repeat costs no
network. Anything it can't resolve (no token, a network hiccup, an unrecognized id) just falls back to
`note` — same as any other emoji outside the map — never a dropped reaction. Spec: §3.6.

## Swipe-replies

Swipe-reply to one of the assistant's messages and it sees which one you meant — the quoted message is
threaded in as `(replying to: "…")`, so you can answer a nudge from three hours ago with just "yes" and
it will know what you're agreeing to. Quoting a file describes it rather than quoting empty text. Spec:
`../docs/telegram-inbound-spec.md` §4.


## Edits

**Fix a typo and the assistant sees the fix.** Editing a message you already sent reaches the
assistant, and what happens depends only on whether it had got to it yet:

- **It hadn't answered it** — the queued message is simply **replaced**. You sent one message, it reads
  one message, corrected. Nothing announces it.
- **It had already answered it** — it can't be un-answered, so the edit arrives marked as an edit of an
  earlier message, and the assistant responds to the correction as a correction rather than reading it
  as you saying nearly the same thing twice. A message it is answering *at that moment* counts as
  answered, deliberately: the turn already has your old text in hand.
- **Nothing actually changed** — ignored. Telegram sends an edit when it attaches a link preview, which
  isn't something you did.

Like reactions, edits arrive only because the poller explicitly asks for `edited_message` updates —
un-named update types are never sent at all, which is exactly how an edit goes invisible: your screen
reads as corrected and the assistant answers the old text, with nothing on either side to show it.
Spec: `../docs/telegram-inbound-spec.md` §6a. (The poller half ships now; the daemon's replace-or-mark
handling arrives with the daemon's inbound wiring.)

## Question pickers — tappable buttons

**When the assistant needs a decision from you, it sends buttons, not a prose list.** No setup: it uses
the same bot and token. Ask for one directly ("give me that as buttons") or the assistant will use one
itself when it has a real choice to put to you.

A question looks like this — the **descriptions are in the message**, the **buttons are the selector**:

```
Which base should the picker branch cut from?

Tap one.

1. origin/develop  (Recommended)
   The integration branch; the daemon deploys from it.

2. main
   The release point — a PR here would target the wrong branch and never deploy.

        [ 1. origin/develop ]
        [ 2. main           ]
```

Why the descriptions aren't *on* the buttons: a Telegram button label truncates silently at
phone width, which would cut off exactly the "and what it costs" half. The number ties a button to its
description.

- **One question per message.** Four questions = four messages. There's no wizard to page through.
- **Tap and it's done** — the choice is folded into that same message (`✓ origin/develop`) and the
  buttons go, so the question becomes its own record in the scrollback instead of a dead keyboard.
- **Multi-select** (when the choices aren't exclusive) shows `☐`/`☑` checkboxes and a **Done** button.
  Toggle as much as you like — **nothing reaches the assistant until you tap Done**, so a mis-tap costs
  you a second tap and nothing else. Done with nothing ticked is a real answer and is read as one.
- **The first option is the recommendation**, always marked `(Recommended)` — unless the question says
  it's a genuinely open pick.
- **A tap always does something.** If a question is too old, or its record is gone, the button says so
  in a popup and the assistant asks you in words instead. It never just quietly fails.
- **Questions expire after 7 days.** A week covers a full weekday/weekend cycle; past that it's better
  to ask again than to act on a week-old answer to a decision that's moved.

Like reactions and edits, taps arrive only because the poller explicitly asks for `callback_query`
updates. Spec: `../docs/telegram-inbound-spec.md` §6b. (`telegram_ask.py` and the poller half ship now;
routing a tap back into the warm session arrives with the daemon's inbound wiring — until then,
`telegram_ask.py resolve` is the door.)

> **Slash commands (`/status` and friends) are not built.** The bot's directives use a `!` prefix
> (`!status`, `!fable`, `!private`), and whether slash commands should sit *beside* those or *replace*
> them is an open decision, not a detail — so it was left out rather than guessed at (§6b.8).

To send one by hand:

```sh
python telegram_ask.py ask --env-file telegram.env \
  --question "Which base?" \
  --option "origin/develop|The integration branch; the daemon deploys from it." \
  --option "main|The release point — a PR here targets the wrong branch."
```

Add `--dry-run` to see the message and buttons without sending, `--multi` for checkboxes.
`telegram_ask.py list` shows what's still pending.

### Where a picker lands — topics, and one of them is the default

**Pickers go into their own Telegram topics rather than into the conversation**, so an unanswered
decision is findable by scrolling one place instead of scrolling back through a day's chat. The routing
table is data, not code: `telegram_topics.py` reads `../references/telegram-topics.json` if you have
one, else the shipped `../references/telegram-topics.example.json`. Copy the example to
`telegram-topics.json` (gitignored — it's your config) to add, drop or retitle a topic, or mint one at
runtime with no file edit at all:

```sh
python telegram_topics.py add projects "Projects"   # create the topic now; --topic projects routes to it
python telegram_topics.py list                      # every purpose, where it came from, its thread id
python telegram_topics.py retire projects           # stop routing to a runtime-added purpose
```

The shipped defaults:

| Topic | What's in it | How it's asked for |
|---|---|---|
| **Pull requests** | merge-approval pickers only | the merge-approval flow passes `--topic pull-requests` explicitly |
| **Decisions** | **every other picker** — anything the assistant needs an answer to | **the default**: no `--topic` at all |
| **Reminders** | reminder nudges (not a picker — they interrupt, so they get their own place) | `telegram_send.py --topic reminders` |

- **The default is the point.** A picker sent with no `--topic` goes to **Decisions**, so nothing has
  to be edited or remembered for a new picker to land where you can find it.
- **`--topic main` sends to the main chat**, for something that genuinely isn't a
  picker-in-a-channel. That's the only way to opt out, and it's deliberately a value of the same
  flag rather than a separate switch.
- **A typo lands in the main chat too, and nothing is refused.** `--topic pull-reqests` resolves to
  no thread and the picker goes to the main chat. There is no allow-list on this flag on purpose: a
  mistyped topic name may cost the thread, never the message.

**What happens with topics switched off — which is the state on a new bot until you flip them.**
Private-chat topics are gated on a toggle in the **@BotFather Mini App** (not the classic
`/mybots → Bot Settings` menu — the switch isn't in there, and no code can flip it). With it off:

- **every picker goes to the main chat exactly as it would with no topics at all** — the
  `sendMessage` payload is byte-identical, asserted in the tests as an equality between two whole
  payloads rather than as *"this one key is absent"*;
- **the daemon says so once per boot** and never again, naming where the switch is;
- nothing is created, nothing is retried, nothing is lost.

The same holds for every other way this can fail: a `getMe` that can't be reached, an unreadable
state file, a topic you deleted, a creation that failed. All of them mean *the main chat*, and the
picker still arrives. **Nothing about a topic is ever a reason a picker doesn't reach you.**

One consequence worth knowing:

- **Deleting a topic in your client is safe.** The next picker gets refused by Telegram, is re-sent
  to the main chat, and the stale id is forgotten so the one after that creates a fresh topic. You
  may end up with an orphaned empty topic if the record is ever lost — the Bot API has no way for a
  bot to list its own topics — which you can delete by hand. No message is ever lost to it. The same
  is true of an ordinary reply: a thread Telegram refuses costs the thread, never the words.

### Talking to the assistant *inside* a topic

**A topic is meant to be a conversation, not just a place pickers get filed.** Type a message inside a
topic and the assistant answers **in that topic**, with **that topic's history** behind it — so you can
hold several long-running subjects in parallel in one chat. The poller already carries the thread id
on every message and `telegram_send.py` already sends into a thread; the per-topic continuity cache
and reply routing are daemon wiring that lands with the daemon's inbound port.

What that means in practice, once wired:

- **Each thread remembers separately.** The rolling continuity a freshly-spawned session is grounded
  with is that thread's turns, so two subjects in two topics never colour each other's answers.
- **The main chat is just another thread**, and Discord and the cockpit share it.
- **A thread with no history yet gets none, and that is correct.** If a cache can't be read for any
  reason, the assistant starts from your message alone rather than borrowing another thread's
  conversation. It may ask what you're referring to; it will not confidently answer the wrong question.

**Not built:** the assistant naming a thread itself. Telegram tells a bot when you created a topic
without naming it, so a possible end state is that you just start typing, a thread appears, and the
assistant labels it once it knows the subject. Today's topics are the ones created and named
explicitly.

## Attachments

**Just send the assistant the file.** A document / photo / voice / audio / video / sticker / video note
you send the bot is
downloaded to `../state/inbox/` and handed over as a local path plus your caption, so the assistant can
open it and answer in context. Spec: `../docs/telegram-inbound-spec.md` §2.

- The daemon passes `--download-dir ../state/inbox`; a bare `telegram_poll.py` **describes** an attachment
  without fetching it, so a manual peek stays read-only.
- **Nothing auto-runs on a file** — the warm session decides what to do with it conversationally
  (summarize it, import it, or just say "got it").
- **~20 MB ceiling.** That's the Bot API's `getFile` limit, not ours. Bigger files (a full health-data
  export, say) aren't downloaded — the assistant tells you so and asks you to drop the file on the
  machine, where `health_import.py --jsons <zip>` takes it directly.
- Filenames are sanitized before they're written (no traversal, no absolute paths), downloads only happen
  for allowlisted chats, and a fetch that fails costs you the file but never the message.
- **A download that fails is not a file that's gone.** The fetch retries first (above); if it still
  can't land it, the message keeps the Telegram `file_id`, the record says the file is still
  re-fetchable, and one command brings it down without you re-sending anything:
  ```sh
  python telegram_poll.py --refetch <file_id> --download-dir ../state/inbox --env-file telegram.env
  ```
  A partial download is never left under the real filename — it lands in a `.part` sidecar and is only
  renamed once the whole body is down.
- Dream prunes `../state/inbox/` nightly (30 days).
- **A location, venue, contact, poll or dice** has no file to fetch; it arrives as a short described
  line (`[shared a location: …]`) rather than vanishing.
- **An album** (several photos from one tap of Send) arrives as one update per photo sharing a
  `media_group_id`; the poller carries that id on every record so the daemon can hand the album over as
  one turn (§6c).

## Notes

- **Peek vs. commit:** run `telegram_poll.py` without `--commit` to inspect messages without consuming
  them; add `--commit` once they're handled so Telegram stops re-sending them.
- **Asleep machine:** Telegram holds updates ~24h, so messages sent while your machine is off are picked
  up on the next poll — same catch-up behavior as the rest of the local stack.
- **Security:** the bot token grants full control of the bot — treat `telegram.env` like a secret. The
  allowlist (`TELEGRAM_ALLOWED_CHAT_IDS`) keeps strangers who find the bot from driving the assistant.


## Troubleshooting — CRLF in `telegram.env` silently mutes bash senders

**Symptom:** notifications sent from **bash** stop arriving, and nothing in the job log says why.
Python senders keep working, so the rest of the stack looks healthy and it reads as "alerts just
stopped."

**Cause.** `telegram.env` is gitignored, so any Windows editor can re-save it with **CRLF** endings
and nothing in git will ever show it. A bash consumer that sources it —

```sh
set -a; . telegram.env; set +a
```

— then carries the trailing `\r` **inside the value** of `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
Interpolate that into a curl URL and curl refuses the request outright:

```
curl: (3) URL rejected: Malformed input to a URL function
```

Nothing is sent. Senders that pipe curl's output to `/dev/null` (or end the line with `|| true`) swallow
even that, which is what makes the drop silent. Python consumers strip the `\r` when they parse the
file, so they are unaffected — that asymmetry is the tell, and it is also why the file looks perfectly
fine when you open it.

**Detect it** — should print `0`:

```sh
tr -dc '\r' < telegram.env | wc -c
```

**Fix the file:** convert it to LF. **Fix the consumer too** — the file is gitignored, so it exists only
on disk and the next re-save can reintroduce the CRLF. The in-script strip is the half that lasts:

```sh
set -a; . telegram.env; set +a
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN//$'\r'/}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID//$'\r'/}
```

**Check the siblings while you're there.** The same editor habit hits every env file it touches
(`google.env`, `push-call.env`, and any shell-side env file a launcher sources). The failure only
surfaces where **bash** reads the value, so a contaminated file can sit harmless for weeks until the
first shell script sources it. Same root cause as any CRLF surprise on a `core.autocrlf=true` machine:
a trailing `\r` that changes what a value *means* rather than how it looks.
