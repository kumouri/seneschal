# Home Assistant / ESPHome action layer (presence feed — Phase 5)

The presence feed already senses **where the owner is and what they're doing** (geofence enter/exit,
driving/still, asleep/awake → `state/presence.db` + `state/presence-context.json`). Phase 5 is the
*output* side: turning a context **edge** into a Home Assistant service call — lights on when they get
home, the fountain pump cut when they leave, and so on — so a smart-home setup (e.g. Home Assistant →
ESP32/ESPHome) hangs off the same presence signal.

**Controlling physical devices is act-high.** So this layer is **off until three things are true**, and
even then it's conservative:

1. Home Assistant is standing and reachable (`ha.env` set).
2. The owner has **approved** a given automation (`"approved": true`) — until then every match is
   draft-and-hold.
3. The daemon integration (below) is explicitly enabled.

A `"failsafe": true` automation (e.g. a water-system **leak cutoff**) stays **ask-high always**, even
approved — it never fires unattended.

Until HA is stood up, the whole layer is **inert**: `ha_client.available()` is `False`, nothing calls out,
and the daemon isn't wired to it. What ships is the tested plumbing, ready to switch on.

## Pieces

| File | Role |
|---|---|
| `ha_client.py` | Stdlib HA REST client — `call_service(domain, service, data)` → `POST /api/services/…`. Reads `ha.env`. |
| `presence_actions.py` | **Pure** edge → automation mapping + `disposition()` (act-low vs ask-high). No I/O, unit-tested. |
| `ha.env` | `HA_URL` + `HA_TOKEN` (gitignored; copy `ha.env.example`). |
| `state/presence-automations.json` | The owner's automations (gitignored; copy `state/presence-automations.example.json`). |

An **edge** is a presence.db event boundary: `{"kind":"geofence","label":"home","transition":"enter"}`,
or `{"kind":"activity","label":"in_vehicle","transition":"exit"}`, or `{"kind":"sleep","transition":
"awake"}`. Each automation's `on` block matches one; `call` is the HA service to invoke.

## Setup (when HA is up)

1. **`ollama`-style env:** `cp ha.env.example ha.env`, set `HA_URL` (e.g. `http://homeassistant.local:8123`
   or a Tailscale/LAN IP) and a long-lived `HA_TOKEN` (HA → your profile → *Long-Lived Access Tokens*).
2. **Automations:** `cp ../state/presence-automations.example.json ../state/presence-automations.json`,
   set your real `entity_id`s. Leave `approved:false` at first.
3. **Smoke-test one call (read-only-ish):**
   ```
   python presence_actions.py --edge '{"kind":"geofence","label":"home","transition":"enter"}'
   ```
   Confirms which automations match and their disposition. With `ha_available:true` and an automation
   flipped to `approved:true`, add `--fire` to actually call HA for that one:
   ```
   python presence_actions.py --edge '{"kind":"geofence","label":"home","transition":"enter"}' --fire
   ```
4. **Approve one automation at a time** (`approved:true`) once you've watched it behave. Failsafes stay
   `approved` *and* `failsafe:true` — still draft-and-hold, by design.

## Daemon integration (the deferred wiring)

Not wired yet — device control is act-high, so leave this off until HA is up. When ready, the hook mirrors
the reminders gate in `sentinel.py`: the daemon already imports presence context each cycle, so on a
**new** presence.db edge it would:

```python
import presence_actions, ha_client
for a in presence_actions.actions_for_edge(edge, presence_actions.load_automations()):
    if presence_actions.disposition(a) == "act-low":      # pre-approved, non-failsafe
        ha_client.call_service(**a["call"])               # fire directly (act-low)
    else:
        enqueue_approval(a)                                # draft-and-hold, per autonomy-policy
```

Edge detection: compare the newest `presence.db` event against the prior one per `kind` (the context
recompute already reads them). Keep it **idempotent** — fire once per edge, not once per poll.

Until that hook is enabled, presence sensing and reminder-gating (Phases 1–4) run exactly as before; this
layer just sits ready. See `../references/autonomy-policy.md` for the act-low / ask-high line.
