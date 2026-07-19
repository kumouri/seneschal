---
description: Health check — the green/yellow/red board over the whole install, plus in-session MCP probes
argument-hint: [optional flags to pass through, e.g. "--probe" or "--send-test"]
---

Run the doctor: the deterministic board from `setup_doctor.py`, completed with the in-session
checks a subprocess can't do (MCP tool auth is per-session). Keep this command thin — the
script owns the file-level checks; don't re-derive them.

1. **The board.**

   ```
   python seneschal/scripts/setup_doctor.py
   ```

   Exit code = the number of RED rows. Offer the two optional deepenings (never run them
   unprompted):
   - `--probe` — live-probes each configured model dial via `claude -p` (~120 s timeout per
     model; subscription-billed, `ANTHROPIC_API_KEY` scrubbed). Offer it; run on a yes.
   - `--send-test` — sends a **real Telegram message** to the owner's chat. **Confirm with
     the owner first, every time** — the flag itself just does it.

2. **In-session MCP probes** — map the script's "verify in-session" rows to real verdicts.
   One cheap probe per *configured* server; skip servers that aren't wired (never OAuth or
   register anything here — that's the `mcp` chapter's job):

   | When | Probe |
   |---|---|
   | store row says notion backend | `notion-get-teams` (or any trivial `notion-*` call) |
   | ledger `mcp:calendar` is done / awaiting-auth-restart | `list_calendars` |
   | `seneschal/scripts/slack-mcp.json` present, or ledger `mcp:slack` done | one `slack_search_channels` (any term) |

   Probe answers → that surface is GREEN. Tools absent from this session or an auth error →
   YELLOW with the fix (`/mcp` → the server → finish the OAuth, or `-> /setup mcp:<server>`).
   Never mark a probe you didn't actually run.

3. **The combined board.** Print the script's board with the MCP verdicts folded in (replace
   the store row's "verify in-session" caveat with the real result; add one row per probed
   server), then the summary line — `N green, N yellow, N red`.

4. **Stamp the ledger** (the SKILL's module-API pattern; statuses and counts only, never
   values):

   ```
   python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['doctor_last'] = {'at': setup_state._now(), 'green': G, 'yellow': Y, 'red': R, 'skip': K}; setup_state.save(s)"
   ```

   (Fill G/Y/R/K from the combined board.)

5. **Close with the reds.** For each RED row, one line: what's broken + its fix pointer, and
   offer to jump ("`/setup env:telegram` now?"). No reds → say so and stop; don't pad.

The request (if any): $ARGUMENTS — pass recognized flags (`--probe`, `--send-test`, `--json`)
through to the script, with the `--send-test` confirmation rule above.
