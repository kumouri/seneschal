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

## Notes

- **Peek vs. commit:** run `telegram_poll.py` without `--commit` to inspect messages without consuming
  them; add `--commit` once they're handled so Telegram stops re-sending them.
- **Asleep machine:** Telegram holds updates ~24h, so messages sent while your machine is off are picked
  up on the next poll — same catch-up behavior as the rest of the local stack.
- **Security:** the bot token grants full control of the bot — treat `telegram.env` like a secret. The
  allowlist (`TELEGRAM_ALLOWED_CHAT_IDS`) keeps strangers who find the bot from driving the assistant.
