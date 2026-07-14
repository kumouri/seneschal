# Google (Calendar + Gmail) bridge — setup

Wires the assistant to **Google Calendar (v3)** and **Gmail (v1)** for one or more Google accounts, over
the raw REST APIs. Standard-library Python only — no `pip install`. One Google Cloud **Desktop-app** OAuth
client serves every account; each account contributes its own durable **refresh token**, stored locally.

Scripts:

| Script | What it does |
| --- | --- |
| `google_common.py` | Shared OAuth/token/HTTP plumbing (imported by the others; not run directly). |
| `google_auth.py` | **One-time** consent per account → stores a refresh token. |
| `gcal_api.py` | Calendar: list calendars, list events, free/busy, get/create/delete events. |
| `gmail_api.py` | Gmail: profile, labels, search/list, read, draft, modify/label, send-on-approval. |

Reading calendar + reading/drafting/labeling mail is **act-low** (the assistant just does it). **Sending
mail** and **writing calendar events** are **act-high / outbound** — the assistant draft-and-holds and only
fires the `send` / `send-draft` / `create-event` subcommands on the owner's approval.

---

## 1. Create the Google Cloud project + OAuth client (once, ~10 min)

1. Go to <https://console.cloud.google.com/> → create a project (e.g. "Seneschal").
2. **APIs & Services → Library** → enable **Google Calendar API** and **Gmail API**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External**.
   - Fill app name / support email / developer email (that's all that's required for personal use).
   - **Add each Google account you'll connect as a Test user** *and*, when you're ready, **Publish app**
     → **In production**. See the refresh-token warning below — publishing is what makes the tokens
     durable. You can remain "unverified"; you'll just click past an "unverified app" warning at consent.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Desktop app**. Name it anything.
   - Copy the **Client ID** and **Client secret**.

> **Refresh-token durability (the one real gotcha).** While the consent screen is in **Testing**, Google
> **expires refresh tokens after 7 days** for the sensitive/restricted scopes Calendar + Gmail use — you'd
> be re-consenting weekly. Move the consent screen to **In production** (unverified is fine for personal
> use) and the refresh token lasts until you revoke it. `google_auth.py` always asks for
> `access_type=offline` + `prompt=consent`, so Google actually returns a refresh token.

## 2. Configure the bridge

```sh
cd seneschal/scripts
cp google.env.example google.env      # google.env is gitignored
```

Fill in `google.env`:

```
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxx
```

Leave the rest blank to accept the defaults (scopes = calendar + gmail.modify + gmail.send + userinfo;
token store = `seneschal/state/google_tokens.json`, which is gitignored; auto-picked loopback port).

## 3. Run the consent flow — once per account

Do this on a machine with a browser, signed in to the account you're connecting.

```sh
python google_auth.py --account personal --env-file google.env
python google_auth.py --account work     --env-file google.env
```

Each run prints a Google URL (and tries to open it), spins up a `http://127.0.0.1:<port>/callback`
listener, and waits. Approve consent in the browser; the refresh token is written to the token store under
that account label. Verify:

```sh
python google_auth.py --list --env-file google.env
```

`--account` is just a **label** — pick whatever names you like (`personal`, `work`, …). Those same labels
are what you pass to `gcal_api.py` / `gmail_api.py` as `--account`.

## 4. Use it

```sh
# Calendar
python gcal_api.py list-calendars --account work --env-file google.env
python gcal_api.py events   --account work --days 7 --env-file google.env
python gcal_api.py freebusy --account work --days 3 --env-file google.env

# Gmail
python gmail_api.py profile --account personal --env-file google.env
python gmail_api.py list    --account personal --query "is:unread newer_than:2d" --max 10 --env-file google.env
python gmail_api.py get     --account personal --id <messageId> --env-file google.env
python gmail_api.py draft   --account personal --to a@b.com --subject "Hi" --body "..." --env-file google.env
```

Every command prints one JSON object with `"ok": true|false`. On an auth error the access token is
refreshed and the call retried once automatically; a persistent failure returns `"ok": false` with the
Google error message.

## Notes

- **Time windows.** `--days` / `--start` / `--end` bound the calendar window; bounds are sent to Google in
  UTC (RFC-3339). Event *results* carry their own timezone from Google. For a precise local day, pass
  explicit `--start`/`--end`.
- **Sending is gated.** `gmail_api.py send` / `send-draft` and `gcal_api.py create-event` / `delete-event`
  are the only mutating/outbound subcommands. Everything else is read-only. Keep to the act-low/ask-high
  policy: draft with `draft`, hold for approval, then `send-draft`.
- **Secrets.** `google.env` and `seneschal/state/google_tokens.json` are gitignored; only
  `google.env.example` and `google_tokens.example.json` are tracked. Never commit a real refresh token.
- **Revoke / re-auth.** To reset an account, revoke the app at <https://myaccount.google.com/permissions>
  and re-run `google_auth.py --account <label>` (Google only returns a refresh token on a fresh consent).
