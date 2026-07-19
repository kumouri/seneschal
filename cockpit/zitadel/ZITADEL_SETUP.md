# Zitadel — setup & operations (the cockpit's IdP)

The identity provider for the seneschald cockpit — design + phase plan in
[`seneschal/docs/cockpit-spec.md`](../../seneschal/docs/cockpit-spec.md). One instance, one human user
(the owner), one OIDC app (the cockpit — the auth code ships in this repo; provisioning the app itself
is the one-time human walkthrough below) — archon UIs never talk to Zitadel directly; they sit behind
the cockpit's session via reverse proxy.

## Layout

| File | Tracked? | What |
|---|---|---|
| `docker-compose.yml` | ✅ | The stack: `zitadel` (pinned tag) + `db` (postgres 17, named volume `zitadel-db`). Compose project: **`seneschald-zitadel`**. |
| `.env.example` | ✅ | Template for the secrets (CHANGE-ME placeholders — generate real values, see the file's own note). |
| `.env` | ❌ gitignored | The real secrets (masterkey, DB passwords, initial admin password). Lives beside the compose file in the checkout you operate from. |

## Operations

Run from this directory (compose auto-reads the sibling `.env`):

```powershell
docker compose -p seneschald-zitadel up -d        # start (or apply compose changes)
docker compose -p seneschald-zitadel ps           # status
docker compose -p seneschald-zitadel logs zitadel # logs
docker compose -p seneschald-zitadel down         # stop (data survives in the zitadel-db volume)
```

- Console: **http://localhost:8480/ui/console** — bound to `127.0.0.1` only. Public exposure via a
  tunnel is **deferred**; when the cockpit goes public, Zitadel's
  `EXTERNALDOMAIN`/`EXTERNALSECURE` move to the real domain + TLS in the same change as the tunnel.
- **Version bumps are deliberate:** the image tag is pinned (`v4.16.1`). Bump the tag in a PR, then
  `docker compose -p seneschald-zitadel up -d` — never track `latest`.
- Wiping for a true rebuild = `down` + `docker volume rm seneschald-zitadel_zitadel-db` (destroys all
  identities — an explicitly confirmed, owner-approved step, always).

## First login (the owner)

1. http://localhost:8480/ui/console
2. Login name **`owner@seneschald.localhost`** (user `owner` in org `seneschald`), password =
   `ZITADEL_ADMIN_INITIAL_PASSWORD` from `.env`. A password change is forced immediately — pick the
   real one and it never needs to live in a file.
3. Enroll **TOTP** right away (Personal settings → Security → One-time password) — the break-glass
   ladder's second rung depends on it. Any standard authenticator app works.

## What the auth stack adds — the code ships in this repo; the app registration below is a one-time HUMAN step

The real auth code (`cockpit/server/auth.py` + `oidc.py`, authorization code + PKCE, no local
JWT/JWKS verification — see that module's docstring) and the break-glass ladder
(`cockpit/breakglass/`) ship here. None of it can turn itself on, though: **no OIDC client id exists
until the owner runs through this walkthrough once.** Until then, `COCKPIT_OIDC_CLIENT_ID` stays
unset and the cockpit answers exactly as it always has (`auth.auth_mode()` falls through to the
`COCKPIT_DEV_NO_AUTH` stub, or 503 if that's unset too — see `cockpit/README.md`'s "Auth modes").

### Provisioning the OIDC application (one-time)

1. **Log in** at http://localhost:8480/ui/console as `owner@seneschald.localhost` (see "First login"
   above — do this first if you haven't; TOTP enrollment isn't required for login to work, but IS
   required before break-glass rung 2 means anything, so do it now if you skipped it).
2. **Create a project.** Console → Projects → "+ New" → name it e.g. `seneschald-cockpit`. A project is
   just a container Zitadel wants before an app can exist under it — nothing else in this repo reads
   its name or id.
3. **Create the OIDC application** inside that project: Applications → "+ New" →
   - **Name:** `seneschald-cockpit` (or anything memorable — not read by any code here).
   - **Type:** *Web*.
   - **Authentication method:** **PKCE** (no client secret) — this is what lets `auth.py` skip ever
     handling a client secret at all; the app registration must NOT generate one.
   - **Redirect URI:** `http://127.0.0.1:8760/auth/callback` — must match
     `COCKPIT_OIDC_REDIRECT`'s default exactly (path included: `/auth/callback`, not
     `/api/auth/callback` — the callback is a top-level browser route, not under `/api`).
   - **Post-logout redirect URI:** `http://127.0.0.1:8760/` (optional; the cockpit doesn't currently
     call Zitadel's own end-session endpoint — `GET /auth/logout` just clears the cockpit's own
     session cookie — but setting this costs nothing and covers a future addition).
   - **Dev mode / allow HTTP:** **ON**. Zitadel refuses a plain-`http://` redirect URI for a
     "production" app by default; this instance is `127.0.0.1`-only, so there's no TLS to
     have. Flip this back off in the same change that stands up a public tunnel (the "Open
     items" entry in the spec).
   - Save. Zitadel shows the app's **Client ID** — copy it now; there is no client secret to copy
     (PKCE apps don't get one).
4. **Grant the owner (the org user) access to the project** if the console doesn't do it automatically
   for the org's own creator — Zitadel sometimes requires an explicit project grant/role for non-admin
   users; as the org's initial admin this is usually already covered, but if login later 403s at
   Zitadel's OWN authorize screen (before it ever reaches this cockpit), check the project's
   Authorizations tab.
5. **Wire it into the cockpit:** copy `cockpit/server/cockpit.env.example` → `cockpit/server/cockpit.env`
   (gitignored) and fill in:
   ```
   COCKPIT_OIDC_ISSUER=http://localhost:8480
   COCKPIT_OIDC_CLIENT_ID=<the Client ID from step 3>
   COCKPIT_OIDC_REDIRECT=http://127.0.0.1:8760/auth/callback
   COCKPIT_ALLOWED_USER=owner
   ```
   (`COCKPIT_SESSION_SECRET` is NOT a config var — the session-signing secret is auto-generated to a
   gitignored `state/cockpit-session-secret` on first use, never hand-set. See `cockpit/README.md`.)
6. **Restart the cockpit backend** (env vars are read fresh per-request by `config.py`, but the
   process needs to have the file sourced/loaded — however you're running it: `uv run uvicorn
   cockpit.server.app:app --env-file cockpit/server/cockpit.env ...`, or export the vars in the
   shell). Visiting http://127.0.0.1:8760/ should now redirect to `/auth/login` → Zitadel's login page
   → back to the dashboard once you sign in.
7. **First real login exercises the allowlist too** — `userinfo.preferred_username` (Zitadel logins
   are typically `<username>@<domain>`, so `auth.py` checks both the full string and the part before
   the `@`) must match `COCKPIT_ALLOWED_USER`. It will for `owner`; anyone else who somehow logs into
   this Zitadel org gets a polite, audited 403 rather than a cockpit session.

### The break-glass step-up flow

Fresh re-auth + TOTP enforced, then the Telegram one-time phrase (spec §Break-glass) — no extra
Zitadel configuration needed beyond the app above: the break-glass ladder's rungs 1-2 reuse the SAME
OIDC app, just with `prompt=login&max_age=0` added to the authorize request so Zitadel forces password
+ TOTP again even with a live session. See `cockpit/breakglass/supervisor.py`'s module docstring for
the full trust chain, and `cockpit/README.md`'s "Break-glass" section for how to run the supervisor.
