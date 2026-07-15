# Discord setup — bot + two-way chat (one-time)

Discord is a full two-way surface for the assistant, the same as Telegram: reminders, nudges, approval
prompts, and a real back-and-forth chat, all in a private Discord channel. The scripts (`discord_send.py`
outbound, `discord_gateway.py` + `discord_poll.py` inbound) are already built — you just need to create a
bot, invite it to a private channel, and drop its token + ids into `discord.env`.

> **Gateway push, with REST fallback + catch-up:** inbound rides the Discord **gateway websocket** —
> messages reach the assistant the moment you send them. This needs the `websockets` dependency from the
> uv-managed venv (`uv sync --frozen`; see the repo `pyproject.toml`); when it's unavailable the daemon
> falls back to REST-polling `GET /channels/{id}/messages?after=<id>` on a ~10 s cadence — degraded, never
> dead. Either way, every (re)connect first runs one REST catch-up against the stored offset, so messages
> sent while the assistant was down/disconnected are never lost. (History: the original build was REST-only
> to stay stdlib-pure — that constraint was retired with the asyncio daemon, see
> `seneschal/docs/asyncio-daemon-design.md`.)

> **Prerequisites:** a Discord account and a server you control (a free personal server is perfect).

## Steps

1. **Create the application + bot.** Go to the **Discord Developer Portal**
   (<https://discord.com/developers/applications>) → **New Application** (name it after your assistant) →
   **Bot** → **Reset Token** and copy the **token**. Keep it secret — it's the bot's password.
2. **Enable the Message Content intent.** On the same **Bot** page, turn on **Message Content Intent**
   (under "Privileged Gateway Intents"). Without it, inbound message text comes back empty.
3. **Invite the bot to your server.** **OAuth2 → URL Generator** → scopes: **bot**; bot permissions:
   **View Channel**, **Send Messages**, **Read Message History**. Open the generated URL and add the bot to
   your server.
4. **Make the assistant's channel.** Create (or pick) a **private** channel just for the assistant and make
   sure the bot can see it. Enable **Developer Mode** (User Settings → Advanced), then **right-click the
   channel → Copy Channel ID**.
5. **Get your own user id.** Right-click your name → **Copy User ID** (Developer Mode on). This goes in the
   allowlist so only you can drive the assistant.
6. **Create `discord.env`** next to these scripts: `cp discord.env.example discord.env` and fill in
   `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, and `DISCORD_ALLOWED_USER_IDS` (your user id).
   `discord.env` is git-ignored — never commit it.
7. **Verify the token** (no message sent):
   ```sh
   python discord_send.py --check-auth --env-file discord.env
   ```
   Should print `{"ok": true, "bot": "...", ...}`.
8. **Round-trip test:**
   ```sh
   python discord_send.py --text "Assistant here — wired up." --env-file discord.env
   ```
   The message appears in the channel. Then post a reply yourself and run
   `python discord_poll.py --commit --env-file discord.env` — your message comes back as JSON and the
   offset advances so it isn't re-read.
9. **That's it — the daemon auto-detects it.** `presence.py` picks up `scripts/discord.env` automatically
   (exactly like `notion-mcp.json`) on its next start/reload; no launcher edit needed. `--no-discord`
   forces it off; `--no-discord-gateway` forces REST polling. Tell the assistant "Discord is up."

## How it's used

- **Outbound** (`discord_send.py`): reminders, proactive nudges, approval prompts, and the assistant's chat
  replies — routed to Discord when a reminder's `channel` is `discord`, or as its reply to an inbound
  Discord message.
- **Inbound** (`discord_gateway.py`, falling back to `discord_poll.py`): new messages wake the same warm
  Chat session that handles Telegram (one persona across `/assistant`, Telegram, Discord, and scheduled
  runs). The assistant replies on the channel the message came from.
- **Offset** lives in `../state/discord-offset` (gitignored) — the last-seen message id, advanced by both
  the gateway and the REST poll so they stay coherent. Delete it to re-seed "from now."

## Notes

- **First run seeds "from now."** With no offset yet, `discord_poll.py` records the newest message id and
  returns nothing, so the assistant doesn't replay the whole channel on first start.
- **Bot messages are ignored.** The poller skips any message whose author is a bot (including the
  assistant's own), so there are no self-echo loops.
- **Security:** the bot token grants full control of the bot — treat `discord.env` like a secret. The
  allowlist (`DISCORD_ALLOWED_USER_IDS`) keeps anyone else in the server from driving the assistant.
