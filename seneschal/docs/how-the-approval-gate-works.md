# How the approval gate actually works

**Status:** `REFERENCE` — a how-it-works page.

## The one sentence that matters

**`../references/autonomy-policy.md` IS the gate** — for everything except two action classes that
are enforced in code. `../references/autonomy-config.json` is a declarative record of the dial (CI
checks that it parses); no script reads it to block anything, so it can never be the thing that stops
an action.

## What "the gate" actually does

The orchestrator (`../SKILL.md`) reads the policy and **follows** it: act-low items get done without
asking; ask-high items get drafted and held for the owner's explicit approval. That is a turn-level
judgment call, exercised fresh every time — no supervisor process reads a turn's plan and vetoes an
ask-high action before it happens.

**So the gate fails open, on every path but two.** If a turn misreads the policy, or skips it under
pressure, nothing catches that for most actions.

## The two exceptions, and why only those

**(1) An outbound send to anyone but the owner** is enforced in code: `../scripts/send_gate.py`. Every
outbound script (`proton_send.py`, `gmail_api.py send`/`send-draft`, `gcal_api.py`
`create-event`/`delete-event`, `push_sms.py`, `push_call.py`, `discord_send.py`) asks
`send_gate.require_approval(kind, recipient)` before its network call, and `send_gate_hook.py` (a
`PreToolUse` hook) puts the same decision in front of the Slack and Gmail MCP send tools. A send to the
owner — resolved against `owner.email` / `owner.emails` in `persona/identity.json`, never a hard-coded
address — passes without reading anything. Anything else needs an **`approved`** row in the ONE
approval store, `../state/pending-approvals.json` (written only through `pending_approvals.py`), or it
is refused: exit 3, nothing sent. The gate **fails closed** — a corrupt store or a bug inside the gate
is a refusal, never a pass. So for sends, the held-approvals ledger is not just something the model
writes: **the `approved` row IS the approval**, and the gate spends it.

**(2) Merging a functionality PR** is enforced by `../scripts/merge_guard.py`, a `PreToolUse` hook with
two stages of **opposite failure polarities** on purpose: stage 1 ("is this even a merge command?")
fails **open**, so a bug there can't brick every shell call on the machine; stage 2 ("may this
identified merge proceed?") fails **closed**, including on its own exceptions — "cannot tell" is never
read as "allow" once a merge has been positively identified.

Why these two and not more: each is an action that cannot be taken back, and each is one where a prose
rule demonstrably stopped holding on its own — a written rule in a memory file does nothing to a future
session, while a check that returns a non-zero exit does. Nothing else in the gate has that history,
so nothing else has that code.

**Both hooks are inert until installed** — `~/.claude/settings.json` is host-side and nothing in this
repo writes it (`../scripts/SEND_GATE_SETUP.md`, `../scripts/MERGE_GUARD_SETUP.md`). The send gate's
script half needs no install: it is in the scripts' own code.

## If you're building something that touches this

- **A written rule is not a check.** Only real code can refuse anything.
- **A new ask-high action needs no new mechanism** — one sentence in `autonomy-policy.md`, and a turn
  that reads it. That's the whole graduation path (see the policy's graduation log).
- **A new outbound channel must call the send gate.** `test_send_gate_wiring.py` reads source text:
  every script that instruments a send (`send_recipients.record`) must also call
  `send_gate.require_approval` before its first network call, or CI fails.
- **Something else that should actually be enforced?** `send_gate.py` (fail closed, one store, a
  refusal line that names the fix) and `merge_guard.py` (two stages, opposite fail polarities, a
  documented reason the asymmetry is deliberate) are the patterns.

Full policy: `../references/autonomy-policy.md`.
