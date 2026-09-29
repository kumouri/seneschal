#!/usr/bin/env python3
"""**A `PreToolUse` hook: the outbound send gate in front of the MCP send tools** (Slack
`slack_send_message`/`slack_schedule_message`, Gmail `send_message`/`reply`/`forward`). Standard
library only; the decision itself is `send_gate.require_approval`, the same one the outbound scripts
call.

The outbound SCRIPTS are gated inside their own code; the MCP tools are not scripts, so the only
place a decision can stand in front of them is a hook. `~/.claude/settings.json` is host-side and
nothing in this repo writes it — **`SEND_GATE_SETUP.md` is the paste**, and until it is pasted this
file changes nothing.

## Polarity: fail CLOSED on a matched tool, fail OPEN on everything else

This hook's worst outcome is a third-party send the owner never approved, so a matched send tool
whose recipient cannot be read is REFUSED with the reason, not waved through. A tool this hook does
not match — every read, every store write, every Bash call — is not its business and is never
touched (exit 0, print nothing). An unreadable event (no JSON, no `tool_name`) cannot be matched and
is likewise left alone; the tool name is the match key, and without it there is nothing to decide.

Block = **exit 2 with the reason on stderr**, Claude Code's blocking-hook convention: a truncated
stdout cannot half-write a JSON decision into an allow.

## What "recipient" is, per tool

* Slack — the channel (`channel_id` / `channel` / `channelId`), kind `slack`. A DM channel is a
  channel; the store row that approves it is a held draft whose `channelRef` reads
  `slack:<channel>[:<ts>]` (what `subagents/slack-triage/SKILL.md` writes) — so `send a7` →
  `resolve a7 --status approved` is the whole approval, and the hook spends it on the way through.
* Gmail `send_message` — `to`/`cc`/`bcc` (string or list), kind `email`, owner-class when every
  address is the owner's own (`send_recipients.self_addresses`).
* Gmail `reply`/`forward` — usually carry no address, only a `thread_id`/`message_id`; those ids are
  the recipient token, matched against a held draft's `channelRef` (the Gmail thread/message id the
  email-triage skill records) or a `grant --kind email --recipient <id>`.
* Gmail `send_message` **by `draftId` alone** (no `to`/`cc`/`bcc`) gets a different refusal: a
  draft's real recipient lives in Gmail's own copy of the draft, not in this call's arguments, so a
  standing grant keyed on the real address can never match it. Unlike a reply/forward's thread id (a
  deliberate, approvable id-gate), there is a cheap way out — send with the recipients spelled out —
  so the refusal names that fix instead of just failing to match.

The consumed approval is stamped BEFORE the tool runs (the hook cannot see the result), the same as
the scripts; a send that then fails needs a fresh grant, and the refusal line says so.

## Purpose-scoped sends: the message is the marker, a ledger is the proof

A **purpose grant** (`send_gate.py grant --kind slack --purpose <slug> --standing`) covers any
recipient of its kind — so the hook must never take the purpose from the sender's say-so, or a label
on any message would open every channel. The label comes from a DETECTOR that owns the purpose and
reads the outgoing message itself, returning the slug only when it can prove the act (for example,
the message is verbatim the confirmation a script composed AND that script's own ledger records the
act it confirms). `_PURPOSE_DETECTORS` is the closed list, and **it ships empty**: adding a purpose
means adding a detector that can prove its act, never a free-text field. A detector that raises
proves nothing, and the ordinary gate applies.

USAGE (the hook reads a `PreToolUse` event on stdin and takes no argv):
    python send_gate_hook.py < event.json
    python send_gate_hook.py --explain '<event json>'      # print the verdict, never consume
"""
from __future__ import annotations

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import send_gate  # noqa: E402
import send_recipients  # noqa: E402

#: The closed list of purpose detectors: `(kind, tool_input, state_dir=...) -> purpose | None`.
#: Each one owns its purpose and must be able to PROVE the act from the message + its own ledger.
#: Empty in the framework — see the module docstring.
_PURPOSE_DETECTORS: tuple = ()

#: tool-name regex → (kind, recipient-extractor). Matched case-insensitively on the full MCP tool
#: name so a differently-prefixed connector (`mcp__claude_ai_Slack__…`, `mcp__slack__…`) still hits.
_SLACK_RE = re.compile(r"slack_(send_message|schedule_message)$", re.IGNORECASE)
_GMAIL_RE = re.compile(r"gmail__(send_message|reply|forward)$", re.IGNORECASE)
#: `send_message` specifically (not `reply`/`forward`) — the one Gmail tool that can send an
#: existing draft by id instead of explicit recipients. See `_unresolved_draft_id` below.
_GMAIL_SEND_MESSAGE_RE = re.compile(r"gmail__send_message$", re.IGNORECASE)


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def classify_call(tool_name, tool_input):
    """`(kind, recipient, recipient_class)` for a matched send tool, or `None` when the tool is not
    one this hook guards. `recipient` may be `None` for a matched tool whose input names nothing —
    that is a refusal downstream, not a pass."""
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    if _SLACK_RE.search(tool_name):
        chan = _first(tool_input, "channel_id", "channel", "channelId")
        return ("slack", chan, "third_party" if chan else "unknown")
    if _GMAIL_RE.search(tool_name):
        addrs = []
        for k in ("to", "cc", "bcc"):
            v = tool_input.get(k)
            if isinstance(v, (list, tuple)):
                addrs.extend(str(x) for x in v)
            elif v:
                addrs.append(str(v))
        if addrs:
            return ("email", addrs, send_recipients.classify_emails(*addrs))
        ref = _first(tool_input, "thread_id", "threadId", "message_id", "messageId", "draft_id", "draftId")
        return ("email", ref, "unknown")
    return None


def _unresolved_draft_id(tool_name, tool_input) -> str | None:
    """The `draftId` when this call is `send_message` sending an existing draft by id with no
    explicit `to`/`cc`/`bcc` — the one shape `classify_call`'s generic ref-match (built for
    `reply`/`forward`'s thread ids) cannot tell apart from a legitimately id-gated send, but that
    has a fix a `reply`/`forward` doesn't: resend with the recipients spelled out. `None` for every
    other matched or unmatched call, including `send_message` with explicit addresses."""
    if not (_GMAIL_SEND_MESSAGE_RE.search(tool_name or "") and isinstance(tool_input, dict)):
        return None
    if any(tool_input.get(k) for k in ("to", "cc", "bcc")):
        return None
    return _first(tool_input, "draftId", "draft_id")


def detect_purpose(kind: str, tool_input, *, state_dir: str | None = None) -> str | None:
    """The first purpose any detector can prove for this call, else `None`. A detector that raises
    proves nothing — the ordinary gate applies."""
    for det in _PURPOSE_DETECTORS:
        try:
            p = det(kind, tool_input, state_dir=state_dir)
        except Exception:  # noqa: BLE001 — never a pass, never a crash
            p = None
        if p:
            return p
    return None


def decide(event, *, consume: bool = True, state_dir: str | None = None):
    """One event → `None` (not our business) or a verdict dict from `send_gate`."""
    if not isinstance(event, dict):
        return None
    call = classify_call(event.get("tool_name"), event.get("tool_input"))
    if call is None:
        return None
    kind, recipient, cls = call
    purpose = detect_purpose(kind, event.get("tool_input"), state_dir=state_dir)
    return send_gate.require_approval(kind, recipient, recipient_class=cls, consume=consume,
                                      channel=f"hook:{event.get('tool_name')}", state_dir=state_dir,
                                      purpose=purpose)


def main(argv=None, stdin=None, stderr=None, stdout=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    err = stderr if stderr is not None else sys.stderr
    out = stdout if stdout is not None else sys.stdout
    if argv[:1] == ["--explain"]:
        try:
            v = decide(json.loads(argv[1]), consume=False)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=out)
            return 2
        print(json.dumps(v, ensure_ascii=False), file=out)
        return 0
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        event = json.loads(raw) if raw and raw.strip() else None
    except Exception:  # noqa: BLE001 — unreadable event: nothing to match, nothing to decide
        return 0
    verdict = decide(event)
    if verdict is None or verdict.get("allowed"):
        return 0
    draft_id = _unresolved_draft_id(event.get("tool_name"), event.get("tool_input"))
    if draft_id:
        # The generic `send_gate.refusal_line` names a `--recipient` to grant — but the token it
        # would print here is the draft id, which is not a recipient anyone should grant against.
        # Say what actually fixes this instead.
        err.write(
            f"send_gate REFUSED: sending draft {draft_id!r} by draftId hides its recipient from "
            f"the gate — Gmail keeps a draft's To/Cc on its own side, not in this call's "
            f"arguments. Resend via send_message with explicit to/cc/bcc (and subject/body) "
            f"instead of draftId, so the gate can match a standing or approved grant for the real "
            f"recipient. If you must send this exact draft by id, grant it explicitly first: "
            f"send_gate.py grant --kind email --recipient {draft_id} --why \"<reason>\".\n")
        return 2
    try:
        err.write(send_gate.refusal_line(verdict) + "\n")
    except Exception:  # noqa: BLE001 — a block we cannot explain is still a correct block
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
