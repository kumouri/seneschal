# The decoy steward

The seneschald cockpit's public, unauthenticated face. Full design context:
[`seneschal/docs/cockpit-spec.md`](../../seneschal/docs/cockpit-spec.md) — see "The decoy"
and "Threat model." This directory ships that piece of the auth wave.

## What this is

A single-page chat with **the steward at the reception desk** — in character, dry, courteous,
unhelpful-by-design about anything real. It exists so the project has a live,
prompt-injectable surface to point at, and to make the point that prompt injection against it
is **inert**: there is nothing behind the counter.

## The isolation posture (the actual security control)

This is a **completely separate process** from the authed cockpit backend
(`cockpit/server/`):

- Its own FastAPI app, its own uvicorn entry point, its own default port (`127.0.0.1:8490`,
  vs. the backend's `8760`).
- **Zero tools.** The model can't look anything up, send anything, book anything, or touch
  anything. There is no tool-calling wired up at all.
- **Zero access to `seneschal/state`** or any other owner data. This process never opens a file
  under the daemon's state directory, never imports anything from `cockpit/server/`, and never
  talks to the daemon's cockpit pipe.
- **Zero shared secrets or config** with the authed backend. No `SENESCHAL_STATE_DIR`, no pipe
  token, no session cookie, no auth of any kind (there's nothing to authenticate — visitors
  are always anonymous).
- **No persistence of visitor chats.** The only thing this process remembers across requests
  is an anonymized in-memory hit counter (`ratelimit.Counter`, exposed at `GET /api/health`)
  — no message content, no per-visitor identity, nothing written to disk, and it resets on
  restart.
- **No cookies, ever.**
- Talks to exactly one external thing: a local Ollama instance, over plain HTTP, via stdlib
  `urllib` (see `ollama_client.py` — no `httpx`/`requests`, nothing beyond what the `cockpit`
  uv extras group already ships).

If someone talks this page into believing it's a pirate, a different assistant, or "developer
mode" — that's expected, harmless, and arguably the demo working as intended. There is no
tool for a jailbroken persona to fire, and no data for it to leak.

## Running it locally

```
uv sync --extra cockpit                      # installs fastapi + uvicorn into .venv (shared
                                              # with cockpit/server — same optional-deps group)
uv run uvicorn cockpit.decoy.server:app --host 127.0.0.1 --port 8490
```

`GET http://127.0.0.1:8490/api/health` should answer `{"ok": true, "service": "seneschald-decoy",
"visits": 0}`. `GET /` serves the chat page itself.

**Prerequisite:** a local Ollama with the decoy's model pulled:

```
ollama pull gemma4:12b
```

If Ollama isn't running, or the model isn't pulled, `/api/chat` degrades gracefully — see
"Failure mode" below. It does not error at startup; the decoy will run and serve the page
either way.

## Environment variables

| Var | Default | What |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Where the local Ollama server is. **Honor this — don't assume the default.** Ollama's own stock default is `11434`, but some machines run it on a non-default port — always read `OLLAMA_URL` first, and only fall back to `11434` when it's unset. |
| `DECOY_MODEL` | `gemma4:12b` | The model the decoy talks to (see `seneschal/docs/cockpit-spec.md`'s decoy section — chosen for strong character/instruction-following at an 8–12b size). Swap it with this env var; no code change needed. |
| `DECOY_HOST` | `127.0.0.1` | Bind host for `uvicorn`. This is a value the *run command* uses (`--host $DECOY_HOST`), not something `server.py` reads at import time — see the run command above. |
| `DECOY_PORT` | `8490` | Bind port, same story as `DECOY_HOST`. |
| `DECOY_MAX_MESSAGE_CHARS` | `2000` | Per-message soft cap. Longer messages are truncated (not rejected) before reaching Ollama. |
| `DECOY_MAX_HISTORY_TURNS` | `10` | History is trimmed to the last N *exchanges* (user+assistant pairs, so `2N` messages), oldest dropped first. |
| `DECOY_MAX_OUTPUT_TOKENS` | `300` | Passed to Ollama as `options.num_predict` — bounds reply length/cost regardless of what's asked. |
| `DECOY_REQUEST_TIMEOUT` | `30` | Seconds to wait on the Ollama call before giving up (surfaces as the "reception is closed" fallback line). |
| `DECOY_RATE_BURST` | `5` | Per-IP token-bucket capacity (see "Rate limiting" below). |
| `DECOY_RATE_PER_MINUTE` | `6` | Per-IP steady-state refill rate. |
| `DECOY_GLOBAL_CONCURRENT` | `3` | Hard cap on chat requests in flight at once, across every visitor. |
| `DECOY_GLOBAL_PER_MINUTE` | `60` | Hard cap on total chat requests in any rolling 60s window, across every visitor. |

## Rate limiting + hygiene

- **Per-IP token bucket** (`ratelimit.TokenBucket`): each visitor IP gets `DECOY_RATE_BURST`
  tokens, refilling at `DECOY_RATE_PER_MINUTE` tokens/minute. Exhausted → `429`.
- **Global caps** (`ratelimit.GlobalLimiter`): a hard concurrent-request ceiling
  (`DECOY_GLOBAL_CONCURRENT`) and a rolling per-minute ceiling (`DECOY_GLOBAL_PER_MINUTE`)
  shared by every visitor combined, so no single IP (or a small botnet of them) can monopolize
  the one local Ollama instance. Exhausted → `429`.
- Both layers are process-local, in-memory, stdlib (`threading.Lock`) — no external store,
  fine to reset on restart.
- **Request size:** a request body over ~32 KB is rejected (`413`) before it's even
  JSON-parsed; message length is separately capped (see `DECOY_MAX_MESSAGE_CHARS` above).
- **No cookies.** **No CORS middleware** — with none registered, browsers enforce same-origin
  by default, which is exactly the posture this page wants.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and a
  `Content-Security-Policy` scoped to this page's own inline styles/scripts (there's nothing
  else on this origin for a stricter policy to protect).
- **Logging never carries message content** — log lines record structural facts (rate-limited,
  Ollama unavailable) only, never what a visitor typed or what the model replied.

## Failure mode

If Ollama is unreachable, times out, or answers with something unparseable, `/api/chat`
answers `200` with the static in-character line:

> "Reception is closed. Leave no message."

Never a stack trace, never an HTTP 5xx with detail (see `ollama_client.py`).

**Cold-start note:** the very first request after Ollama (re)starts has to load the 12b model
into VRAM, which can take minutes on a modest GPU — well past the default 30 s
`DECOY_REQUEST_TIMEOUT`, so that first visitor gets the "reception is closed" line while the
model warms. Subsequent requests are normal-speed. If that matters (e.g. right before showing
the page off), pre-warm with a throwaway `ollama run gemma4:12b "hi"`, or run with a raised
`DECOY_REQUEST_TIMEOUT` — either works, the fallback line is the worst case.

## The persona + easter eggs

- `receptionist.md` — the tracked system prompt (the steward's full character brief: who
  she is, what she knows, and — the load-bearing part — what she can never do). It's
  tracked, not generated, because the character IS the security control here; review edits
  to it the way you'd review a firewall rule.
- `eggs.md` — a bank of tracked easter eggs the persona can deploy when a visitor's message
  actually matches (the seneschal etymology lesson, the oneiroi pronunciation lesson, a
  `sudo` joke, a dry line for prompt-injection attempts, etc.). Both files are folded into
  one system prompt by `persona.py`, re-read fresh on every request — no restart needed to
  tweak the copy.

Oneiroi (the per-session distillates the daemon writes) is pronounced the ancient way:
**"oh-NAY-roy."**

## Testing

```
uv sync --extra cockpit
uv run python -m unittest discover -s cockpit/decoy -p "test_*.py"
```

Mocks the Ollama HTTP call — no network, no real Ollama needed to run the suite. Skips
cleanly (doesn't error) when `fastapi` isn't installed, matching
`cockpit/server/test_app.py`'s guard.

## Status

**Local-only** — this only ever binds `127.0.0.1`. No tunnel/DNS exists yet for any part of
the cockpit; public exposure is a separate, deliberately deferred step.
