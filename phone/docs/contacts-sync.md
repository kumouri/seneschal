# Google Contacts → allowlist auto-sync

Keeps the screener's allowlist continuously in sync with your Google Contacts, hands-off. Contacts ring
straight through (no screening); anyone you remove from Google drops off the allowlist on the next sync.
Numbers you add by hand (`source = 'manual'`) are preserved.

## How it works

A tiny **Google Apps Script in your own account** reads your contacts hourly and POSTs them to the Worker's
`POST /sync-contacts` endpoint (bearer-authenticated). The Worker reconciles them into the D1 `contacts`
table as `source = 'google'`. This avoids Google's OAuth-app verification and the 7-day refresh-token expiry
entirely — the script runs as *you*.

```
Apps Script (hourly, runs as you) ──POST {contacts:[…]} + Bearer secret──► Worker /sync-contacts ──► D1 contacts
```

## One-time setup (~15 min)

1. **Create the sync secret.** Generate a long random value (e.g. `openssl rand -hex 32`), set it on the
   Worker with `wrangler secret put CONTACTS_SYNC_SECRET`, and keep a copy in your local `.dev.vars`
   (gitignored). (To rotate it later: set a new value the same way and update the Apps Script property
   to match.)

2. **Create the Apps Script.** Go to https://script.google.com → **New project** → delete the stub and
   paste the contents of [`scripts/google-contacts-sync.gs`](../scripts/google-contacts-sync.gs).

3. **Enable the People API.** In the editor's left rail, click **Services (+)** → choose **People API** →
   **Add**.

4. **Set Script Properties.** **Project Settings** (gear) → **Script Properties** → add:
   - `SCREENER_URL` = `https://seneschal-screener.YOUR-SUBDOMAIN.workers.dev/sync-contacts`
   - `SYNC_SECRET` = the `CONTACTS_SYNC_SECRET` value from `.dev.vars`

5. **Authorize + first run.** Select the `syncContacts` function → **Run**. Approve the permission prompt
   (it asks for read access to your contacts — that's the script accessing *your own* data). Check the
   execution log: it should say `Pushed N contacts -> HTTP 200`.

6. **Schedule it.** **Triggers** (alarm-clock icon) → **Add Trigger** → function `syncContacts`,
   event source **Time-driven**, type **Hour timer**, **Every hour**.

## Verify

```bash
# from the repo, with CLOUDFLARE_API_TOKEN set:
wrangler d1 execute call_screener --remote --command \
  "SELECT count(*) AS n, source FROM contacts GROUP BY source"
```
You should see a `google` row with your contact count. Then call the screener from a number that's in your
contacts — it should ring straight through with **no** press-1 gate.

## Notes

- The endpoint rejects any request without the correct `Authorization: Bearer <secret>` (401).
- Re-running the sync is idempotent: it upserts current contacts and removes Google-sourced numbers that are
  no longer in your contacts. Manual allowlist entries are never touched.
