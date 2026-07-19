#!/bin/sh
# =============================================================================
# Seneschal presence daemon launcher (POSIX — cron / systemd / launchd).
#
# TRACKED TEMPLATE — this file (and its Windows sibling run-presence.cmd) is the
# checked-in launcher template the setup wizard (subagents/persona-wizard)
# renders a per-machine local launcher from. Keep it machine-neutral: no real
# usernames, absolute per-host paths, or secrets — those belong in the local
# copy the wizard writes (or in your user environment), never here.
#
# Point your supervisor at THIS file (no arguments needed), e.g.:
#   systemd:  ExecStart=%h/workspace/repos/seneschal/seneschal/scripts/run-presence.sh
#             (Restart=on-failure; one instance — the daemon's own lock also guards this)
#   cron:     @reboot $HOME/workspace/repos/seneschal/seneschal/scripts/run-presence.sh
#
# This wrapper exists so you don't fight supervisor quoting — edit the knobs
# here, in a normal file, instead of in a unit/crontab line. It also guarantees
# SUBSCRIPTION billing (not metered API):
#   * It clears ANTHROPIC_API_KEY for this process tree.
#   * It relies on CLAUDE_CODE_OAUTH_TOKEN being set in the environment
#     (one-time:  claude setup-token   then export it where this runs).
# =============================================================================
set -u

# --- billing safety: never let a stray API key force metered API billing ---
unset ANTHROPIC_API_KEY

# --- force UTF-8 so emoji / em-dashes in replies never mojibake ---
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
export PYTHONUTF8 PYTHONIOENCODING

# --- run from the repo root so the daemon's relative paths resolve ---
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/../.." || exit 1

# --- OPTIONAL add-ons (mirror run-presence.cmd; append the flag to the command below) ---
#  * Store MCP for HEADLESS runs: resolved automatically from seneschal/store/config.json
#    (backends[active].mcp_config — run /setup-store), falling back to a legacy
#    scripts/notion-mcp.json. Override:  --store-mcp /path/to/mcp.json   ·  force OFF:  --no-notion
#  * Discord two-way channel (DISCORD_SETUP.md): AUTO-DETECTED — just create scripts/discord.env
#    (gateway push when the venv is live, REST fallback otherwise). To force OFF:  --no-discord
#  * Phone-call reminders (Call Me):  --call-env "$SCRIPT_DIR/push-call.env"
#  * Internal scheduled runs (brief/wrap/dream/journal + the once-per-day exact-time reminder SEED)
#    are ON by default; --no-slots disables them all, --no-seed-day disables only the seed,
#    --slot-model <id> runs them on a specific model.
#  * Cockpit pipe (localhost websocket, seneschal/docs/cockpit-spec.md): ON by default when the venv's
#    websockets is importable; --no-cockpit forces it off, --cockpit-port <n> moves it.

# --- interpreter: prefer the uv-managed venv (has the daemon's dependencies, e.g. websockets),
#     fall back to system python3 (the daemon degrades gracefully without the venv — the Discord
#     gateway and the cockpit pipe fall back/off). `uv sync --frozen` creates .venv.
PYEXE="$SCRIPT_DIR/../../.venv/bin/python"
[ -x "$PYEXE" ] || PYEXE=python3

# --- knobs (edit these freely) ---
#  * --model below is the FALLBACK warm-session model only. The LIVE knob (v3, cockpit-spec.md
#    "Model dials & Fable delegation") is state/model-config.json (via the cockpit's Model dials
#    panel, or seneschal/scripts/model_config.py): its warm_model wins over --model at every
#    warm-session spawn, and max_routable_model is the live ceiling on Fable delegation. Edit --model
#    here only to change the fallback used when no model-config.json (or no warm_model in it) exists.
exec "$PYEXE" "$SCRIPT_DIR/presence.py" \
  --model claude-opus-4-8 \
  --idle-min 20 \
  --poll-timeout 25 \
  --peek-interval-min 5 \
  --call-env "$SCRIPT_DIR/push-call.env" \
  --log-file "$SCRIPT_DIR/../state/presence.log" \
  --watch-model claude-haiku-4-5-20251001 \
  --watch-prompt "Run Watch mode (seneschal/SKILL.md): glance at email/Slack/calendar and escalate only if something is genuinely hot; otherwise exit."
