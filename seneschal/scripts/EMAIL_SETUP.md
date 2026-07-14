# Assistant email setup — Proton Mail Bridge (one-time)

The assistant sends/reads as its own address (e.g. **`assistant@example.com`**) through **Proton Mail
Bridge**, which exposes Proton over a local SMTP + IMAP server. This is the one piece Claude can't do for
you — it needs the Bridge app running on your machine with the assistant's alias configured. Everything
else (the `proton_send.py` / `proton_read.py` scripts and the email-triage skill) is already built.

> **Prerequisites:** a **paid Proton plan** (Bridge requires Mail Plus / Unlimited), the assistant's
> address (e.g. `assistant@example.com`) set up as an **address/alias** on your Proton account, and the
> **Proton Mail Bridge** desktop app.

## Steps

1. **Install & sign in to Proton Mail Bridge** (https://proton.me/mail/bridge). Sign in with the Proton
   account that owns your domain / the assistant's address.
2. **Confirm the address is available in Bridge.** Bridge serves the account's addresses; make sure the
   assistant's address exists (add it in Proton account settings → Addresses if needed). Keep Bridge
   **running** (it can launch on login) — the scripts talk to it locally.
3. **Read the mailbox details.** In Bridge, select the account → **Mailbox details / Configuration**.
   Note: SMTP host/port (default `127.0.0.1:1025`), IMAP host/port (default `127.0.0.1:1143`), the
   **username**, and the **Bridge-generated password** (this is NOT your Proton login password).
4. **Create `proton.env`** next to these scripts: `cp proton.env.example proton.env` and fill in
   `PROTON_BRIDGE_USER` + `PROTON_BRIDGE_PASS` (and any host/port that differ). `proton.env` is
   git-ignored — never commit it.
5. **Verify the connection** (no mail sent/changed):
   ```sh
   python proton_send.py --check-auth --env-file proton.env
   python proton_read.py --check-auth --env-file proton.env
   ```
   Both should print `{"ok": true, ...}`. Then a real read test:
   ```sh
   python proton_read.py --mailbox INBOX --limit 5 --env-file proton.env
   ```
6. **Tell the assistant "Proton is up."** It will run the email-triage skill against the live inbox; the
   first real send still goes out only on your approval (ask-high).

## Notes

- **Self-signed cert:** Bridge uses a local self-signed TLS cert. The scripts default to
  `*_INSECURE=1` (accept it — the connection is to `127.0.0.1`). If you've installed Bridge's CA into
  your system trust store, set them to `0`.
- **Unattended/scheduled runs:** Bridge must be running for scheduled sends/reads to work. If a
  scheduled run can't reach Bridge, the assistant falls back to the Gmail MCP (draft-only) and notes it.
- **Security:** the Bridge password grants access to the mailbox — treat `proton.env` like a secret.
