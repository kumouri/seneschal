# Run the warm session on Codex CLI instead of Claude Code CLI (one-time host setup)

**Why this exists.** `docs/pluggable-backend-spec.md` adds a second `Backend` the warm session can run
on — OpenAI's Codex CLI, on a ChatGPT subscription, no API token — selected from the cockpit's model
dials. **None of this happens automatically.** The daemon's default backend stays `claude-cli` until
you flip the cockpit's Model dials panel yourself (`backend` selector,
`docs/pluggable-backend-spec.md` §3.2) — a merged PR never switches it. This doc is the four host-side
steps a PR cannot do, mirroring `SEND_GATE_SETUP.md`/`NOTION_MCP_SETUP.md`'s own pattern (everything
here is outside version control by design: `~/.codex/*`, like `~/.claude/settings.json`, is never
touched by a merge).

## 1. Install and log in (interactive, once)

1. Install Codex CLI per OpenAI's current instructions, then confirm it runs with `codex --version`.
2. Log in against your ChatGPT subscription, mirroring `claude /login`'s own interactive-once pattern:
   ```sh
   codex login
   ```
   This opens a browser window once; afterward the credential is cached to `~/.codex/auth.json`
   (`auth_mode: "chatgpt"`, `OPENAI_API_KEY: null` — verify with `codex login status`, which should
   print `"Logged in using ChatGPT"`). Every headless `codex exec` call after this reads the cached
   credential with no further browser step, the same shape `CLAUDE_CODE_OAUTH_TOKEN` gives the
   claude-cli backend.
3. **`--codex-bin`**: `presence.py`'s daemon may inherit a pre-install PATH (the same reason
   `--claude-bin` exists), so `codex` alone may not resolve for a process launched the way the daemon
   launches. Pass the full path to the installed binary — e.g.
   `--codex-bin "<install dir>/codex.exe"` on Windows or `--codex-bin "$(command -v codex)"` resolved
   once on Linux/macOS — via whatever wraps `presence.py`'s invocation (`run-presence.cmd`, the systemd
   unit or the launchd plist the `/setup` daemon chapter rendered). Until PATH is updated for the
   daemon's environment, the bare default (`"codex"`) will fail to spawn.

## 2. Grant your store's MCP (and ONLY that) via `~/.codex/config.toml`

Codex reads its MCP servers from a **standing file**, not a per-invocation flag the way `--mcp-config`
is for claude-cli. Filesystem store backends (Obsidian, Markdown) need no MCP at all — skip this step.
On the Notion backend, write it once:

```sh
codex mcp add --transport http notion https://mcp.notion.com/mcp
```

Authenticate the same way the claude-cli path does (`NOTION_MCP_SETUP.md` Path A) — run `codex` once
interactively and complete the OAuth flow when it asks about the `notion` server.

**Never add `slack` or `gmail` here.** This is not a suggestion enforced by convention — it is the
whole safety mechanism `backends/codex_cli.py`'s module docstring documents: `CodexWarmSession`
contains no code path that can grant an MCP server at spawn time at all (unlike claude-cli's
`--mcp-config`), so the ONLY way a codex-backed assistant could ever reach an outbound Slack/Gmail MCP
tool is if this file names one. Per `docs/pluggable-backend-spec.md` §1.4/§2.1: Codex's own
`PreToolUse`-equivalent hook coverage for MCP tool calls specifically is **unverified** — so until
that question is independently re-checked against Codex's own current docs, `slack`/`gmail` MUST NOT
be added here, full stop. (Outbound sends from the framework's own scripts — `proton_send.py`,
`push_sms.py`, etc. — are unaffected either way: `send_gate.py`'s code-level gate lives inside those
scripts themselves, reachable from any backend's Bash tool, and gates identically regardless of which
CLI is running the turn.)

## 3. The `PreToolUse`-equivalent hook — not yet registered

`docs/pluggable-backend-spec.md` §2.1 flags Codex's own hooks system as having *at least some* deny
capability, but whether it fires for MCP tool calls specifically (as opposed to Bash only) was
**unverified** in the spec's own pass. Until that's independently re-checked, there is nothing to
register here — §2 above already closes the gap it would have covered (no MCP send tool is ever
reachable in the first place). If a future change verifies Codex's `PreToolUse` coverage and adds a
`send_gate_hook.py` equivalent for it, this section is where its registration steps belong.

## 4. Flip the cockpit's backend dial

Once steps 1-2 are done: open the cockpit's Model dials panel, change **Backend** to "Codex CLI", pick
a warm model (the panel's dropdown is served live from `cockpit/server/model_config.py`'s
`known_backends` — see that module's own comment for where the codex-cli model list came from and its
honesty caveat about ordering), Save, then **Apply now** (the same graceful-restart button the
claude-cli dial already uses). The daemon picks up the new backend at its next natural respawn or
immediately via Apply now — never mid-turn.

## Verify

1. `codex login status` prints `"Logged in using ChatGPT"`.
2. `codex mcp list` shows `notion` (Notion backend only) and nothing else.
3. After flipping the cockpit dial and applying: message the assistant on Telegram. The daemon log
   should show a codex spawn (`_sandbox_for`'s chosen sandbox, no `--mcp-config` flag anywhere in the
   argv — that flag doesn't exist for codex) rather than a `claude -p` one.
4. On the Notion backend, ask the assistant to read something from the store — should work (the
   standing config grants it). Ask it (or have Slack/email triage try) to send a message to anyone
   but the owner — must be refused, because there is no Slack/Gmail MCP tool mounted at all on this
   backend, by construction.
5. Flip the dial back to "Claude Code CLI" and Apply now to return to the default.

## Notes

- **Billing safety**: `backends/codex_cli.py`'s child env scrubs `OPENAI_API_KEY` from every codex
  spawn, mirroring the daemon's own `ANTHROPIC_API_KEY` scrub — a stray key in your shell environment
  can never silently switch this backend to metered API billing.
- **`codex exec` reads stdin when attached, always** — if you ever shell out to `codex exec` by hand
  for debugging, close/redirect stdin (`< /dev/null` on a Unix-like shell) or it will print `"Reading
  additional input from stdin..."` and hang.
- Fable delegation (`fable_delegate.py`) refuses cleanly on a non-claude-cli backend — it's a
  Claude-family mechanism with no meaning on Codex; the cockpit ceiling dial still saves, it just never
  admits Fable while the backend is codex-cli.
- `usage_probe.py` (`/usage`, a Claude Code CLI slash command) and `read_cli_version()`
  (`claude --version`) have no codex-cli equivalent characterized yet — plan tracking degrades to
  absent on this backend, not a crash.
