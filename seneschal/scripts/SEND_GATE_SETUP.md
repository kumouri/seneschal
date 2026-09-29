# The send gate — the host-side steps

`send_gate.py` is the outbound approval gate: every outbound chokepoint asks
`send_gate.require_approval(kind, recipient)` before its network call. A send to the owner
(`recipient_class == owner`) passes untouched and never reads the store; anything else needs an
**approved** row in the ONE approval store, `state/pending-approvals.json`, or it is refused — exit
3, nothing sent, one line naming the fix. How the whole act-low / ask-high loop fits together:
`../references/autonomy-policy.md` and `../references/comms-mapping.md` → "The send gate".

The six outbound scripts (`proton_send.py`, `gmail_api.py send`/`send-draft`, `gcal_api.py
create-event`/`delete-event`, `push_sms.py`, `push_call.py`, `discord_send.py`) are gated in their
own code — nothing to install. **What is yours, because nothing in the repo can do it:**

0. **tell the gate who you are** — your addresses in `persona/identity.json`;
1. any **standing grant** for a recipient you pre-clear for a recurring send — the address lives in
   the gitignored store, never in a tracked file;
2. the **`PreToolUse` hook** in front of the MCP send tools (Slack, Gmail), which lives in
   `~/.claude/settings.json`.

---

## 0. Your addresses (`persona/identity.json`)

The gate decides "is this the owner?" from configuration, never code:

```json
"owner": {
  "email": "you@example.com",
  "emails": ["you@work.example", "you.other@example.org"]
},
"assistant": { "email": "assistant@example.com" }
```

- `owner.email` + every entry of `owner.emails` count as you (matched case-insensitively). List every
  address of yours a send might legitimately go to — a second mailbox, a work address.
- `assistant.email` (the assistant's own send-from address) counts as self too: a cc to the assistant's
  own mailbox reaches no third party.
- **Nothing configured means every address is a third party** — the fail-closed direction. The
  Brief's own email to you would then be refused until you add your address; `/setup` asks for it in
  the owner interview.

Check what the gate sees (dry — never spends anything):

```sh
python seneschal/scripts/send_gate.py check --kind email --recipient you@example.com
```

`"reason": "owner"`, exit 0, means you are recognised.

## 1. Standing grants for pre-cleared recipients

For a recipient you want a recurring send to reach without a tap each time (a monthly report to an
accountant, a digest to a collaborator), mint one standing grant on the host:

```sh
python seneschal/scripts/send_gate.py grant --kind email --recipient "<their address>" --standing --why "<why this recipient is pre-cleared>"
```

Then confirm the gate would let it through:

```sh
python seneschal/scripts/send_gate.py check --kind email --recipient "<their address>,you@example.com"
python seneschal/scripts/send_gate.py list --standing
```

`check` prints the verdict (`"reason": "standing"`) and exits 0; it never spends anything. A standing
grant is never consumed; `revoke <id> --why "..."` withdraws it. `--expires-days N` or `--expires-at
YYYY-MM-DD[THH:MM:SSZ]` (UTC) makes it lapse on its own.

**A one-off for anything else** (you said "send it" in chat and there is no held draft to approve):
the same `grant` without `--standing` — single-use, spent by the next matching send. For a held
draft the normal path is `pending_approvals.py resolve a<N> --status approved` on your `send a<N>`
(what email and Slack triage do first) — the gate spends that row.

**Purpose-scoped grants** (`grant --kind K --purpose <slug> --standing`, recipient `*`) cover any
recipient of that kind, but only a send the hook labels with that purpose — and the hook only takes
the label from a detector that can prove the act (`send_gate_hook.py`'s `_PURPOSE_DETECTORS`). The
framework ships no detectors, so a purpose grant does nothing until one is added in code.

## 2. Register the hook in `~/.claude/settings.json`

A **`"PreToolUse"`** entry inside the top-level `"hooks"` object. The matcher covers the Slack and
Gmail send tools only; the script re-checks the tool name internally, so widening the matcher is safe
and narrowing it is the only way to weaken it. Replace `$REPO` with the absolute path of your
checkout (the daemon's, the one that runs off `main`):

```json
    "PreToolUse": [
      {
        "matcher": "mcp__.*(slack_send_message|slack_schedule_message|Gmail__send_message|Gmail__reply|Gmail__forward)",
        "hooks": [
          {
            "type": "command",
            "command": "python $REPO/seneschal/scripts/send_gate_hook.py",
            "timeout": 10
          }
        ]
      }
    ],
```

If a `"PreToolUse"` array already exists, add this object **inside that array** rather than a second
`"PreToolUse"` key, which JSON would silently let the last one win.

Check it parses, then verify the mechanism without a session:

```sh
python -c "import json, os; json.load(open(os.path.expanduser('~/.claude/settings.json'), encoding='utf-8')); print('settings.json parses')"
python seneschal/scripts/send_gate_hook.py --explain '{"tool_name":"mcp__claude_ai_Slack__slack_send_message","tool_input":{"channel_id":"C000","text":"x"}}'
python seneschal/scripts/send_gate_hook.py --explain '{"tool_name":"mcp__claude_ai_Gmail__send_message","tool_input":{"to":"you@example.com","body":"x"}}'
```

The first prints `"allowed": false, "reason": "no-approval"` (no draft is approved for `C000`); the
second `"allowed": true, "reason": "owner"` (once step 0 names your address). `--explain` never spends
an approval. In a live session the hook blocks with **exit 2 and the refusal line on stderr**; a tool
it does not match is never touched.

## Polarity, so nobody "fixes" it the wrong way

* Scripts and hook: **fail CLOSED** for a non-owner recipient — a corrupt store, a bug in the gate,
  an unreadable recipient on a matched send tool are all refusals. This is the reverse of
  `send_recipients.record` (instrument-only, never costs a send), and it is deliberate: the failure
  being guarded is a third-party send the owner never approved.
* Owner-class never reads the store, so nothing here can slow a reminder, a picker, a job push or the
  Brief's own email to the owner.
* An approval is spent BEFORE the network call (the scripts and the hook alike cannot see the
  result), so a send that then fails needs a fresh approval — the refusal line says so.
* `gmail_api.py send-draft` and `gcal_api.py delete-event` cannot see their recipients locally; they
  gate on the draft id / event id. A Gmail MCP `send_message` by `draftId` alone is refused with a
  pointer to resend with explicit `to`/`cc`/`bcc`, since a draft's recipient is invisible to the gate.

## What it records

Every gated attempt — allowed or refused — writes a non-content row to `state/send-recipients.jsonl`
(`send_recipients.py`): `{at, channel, recipient_class, gate: {allowed, reason, approval_id}}` —
never the address, number, subject or body. `send_gate.py blast-radius --days 7` reads it back:
counts per channel and class, and for each non-owner row whether it was blocked.
