# Telegram setup — bot + two-way chat (one-time)

Telegram is the assistant's free, always-with-you surface: reminders, nudges, approval prompts, and its
side of an actual back-and-forth chat all flow through a Telegram bot. It costs nothing and works from
your phone. The scripts (`telegram_send.py` outbound, `telegram_poll.py` inbound) are already built — you
just need to create the bot and drop its token into `telegram.env`.

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
  the assistant's chat replies.
- **Inbound** (`telegram_poll.py`): the sentinel polls each cycle with `--commit`; new messages wake the
  full brain in **Chat mode** to reply (one persona across `/assistant`, Telegram, and scheduled runs).
- **Offset** lives in `../state/telegram-offset` (gitignored). Delete it to replay the last ~24h.

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

## Attachments

**Just send the assistant the file.** A document / photo / voice / audio / video you send the bot is
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
- Dream prunes `../state/inbox/` nightly (30 days).

## Notes

- **Peek vs. commit:** run `telegram_poll.py` without `--commit` to inspect messages without consuming
  them; add `--commit` once they're handled so Telegram stops re-sending them.
- **Asleep machine:** Telegram holds updates ~24h, so messages sent while your machine is off are picked
  up on the next poll — same catch-up behavior as the rest of the local stack.
- **Security:** the bot token grants full control of the bot — treat `telegram.env` like a secret. The
  allowlist (`TELEGRAM_ALLOWED_CHAT_IDS`) keeps strangers who find the bot from driving the assistant.
