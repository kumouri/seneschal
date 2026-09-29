#!/usr/bin/env python3
"""The outbound approval gate — `require_approval(kind, recipient)` at every outbound chokepoint,
reading the ONE approval store, `state/pending-approvals.json`. Stdlib only.

Ask-high is a policy (`../references/autonomy-policy.md`); this module is where it is enforced for
the one action class that cannot be taken back — a message leaving the machine for someone who is
not the owner. There is no second approvals store: the triage skills hold drafts in
`pending-approvals.json` through `pending_approvals.py`, the owner's `send a<N>` flips a row to
`approved`, and this gate reads (and spends) it.

## The decision

1. **`recipient_class == "owner"` passes untouched.** A send to the owner — the owner's own
   addresses (`owner.email` / `owner.emails` in `persona/identity.json`, via
   `send_recipients.self_addresses`), the owner's own phone (`push_sms`/`push_call` with no `--to`
   override), the assistant's one private Discord channel, an attendee-less calendar event — is
   act-low. The store is not even read for it, so nothing in this module can slow or refuse a
   reminder, a picker, a job push, or the Brief's own email to the owner.
2. **Anything else needs an APPROVED row in the store that matches `(kind, recipient)`.** Three row
   shapes satisfy it, all living in the one store:
   * a **single-use** approval — a held draft (`kind: "email"` / `"slack"` / …, minted by email or
     Slack triage through `pending_approvals.add`) whose `status` the owner's `send a<N>` flipped to
     `"approved"` (`pending_approvals.resolve a<N> --status approved`). Matching is on the row's own
     recipient fields (`to`/`cc`/`bcc`/`recipient`/`recipients`, `draftId`, `eventId`, or the channel
     part of a `slack:<channel>[:<ts>]` `channelRef`): the row's recipients must COVER every non-owner
     recipient of this send. It is spent on the way through — the gate stamps `gate_consumed_at` +
     `gate_channel` (via `pending_approvals.resolve`, still the one writer) and never matches it
     again. Exact-target and single-use: an approval is the id AND the exact recipient pair.
   * a **standing** approval — `{kind, to, status: "approved", standing: true, reason, granted_by,
     granted_at[, expires_at]}`, minted only by this module's `grant --standing` CLI, never consumed.
     This is how a recipient the owner has PRE-CLEARED for a recurring send gets through without a
     tap per send. The address lives in the gitignored store, granted once on the host — never a
     constant in a tracked file.
   * a **purpose-scoped standing** approval — the standing row above with a `purpose` slug and
     `to: "*"`: `{kind, to: "*", purpose, status: "approved", standing: true, ...}`, minted by
     `grant --purpose <slug> --standing`. It covers ANY recipient of that kind — but only a send the
     CALLER labels with that purpose (`evaluate(..., purpose=...)` / `check --purpose`), and the
     label is never the sender's word: `send_gate_hook.py` derives it from the outgoing message
     itself through a detector that owns the purpose and can prove the act from its own ledger. A
     purpose row is skipped entirely by the recipient match, so it can never widen an ordinary send.
     The framework ships no detectors; the mechanism is the extension point.
3. **Otherwise: REFUSED, fail-closed.** Nothing is sent, the exit is non-zero (`EXIT_REFUSED` = 3
   from every CLI), and the one line printed names the missing approval and how to grant it. A store
   that cannot be read (`pending_approvals.CorruptStore`) refuses too — the polarity is the opposite
   of `send_recipients.record`'s, and deliberately: the ledger instruments and must never cost a
   send; the gate must never let one through on a guess.

`unknown` is NOT owner. `gmail_api.py send-draft` and `gcal_api.py delete-event` cannot see their
recipient locally; they gate on the draft id / event id instead, so approving one means a row that
names that id (`grant --kind email --recipient <draftId>` / `--kind calendar --recipient <eventId>`,
or a held draft that recorded `draftId`). An explicit `--to`/`--channel-id` override on the three
single-recipient channels gates on that number / channel id the same way.

## Never raises into a send — but a raise is a refusal, not a pass

`require_approval` wraps its own evaluation: an unexpected exception becomes a refused verdict
(`reason: "gate-error"`), so a bug here can crash nothing and can let nothing through. The call
sites print the refusal as JSON and return `EXIT_REFUSED` themselves, so a caller matching on the
JSON shape reads `"refused": "send_gate"` and the grant command.

## The measurement it leaves behind

The gate returns a verdict dict; each call site hands it to `send_recipients.record(...,
gate=verdict)`, so the non-content send ledger row gains `gate: {allowed, reason, approval_id}` — no
second ledger, and `blast-radius` reads that same file to answer "what would have been / was
blocked".

## What this does NOT cover

The MCP send tools (Slack `slack_send_message`, Gmail `send_message`/`reply`/`forward`) are not
scripts in this directory; `send_gate_hook.py` is the `PreToolUse` hook that puts this same decision
in front of them, and its REGISTRATION is host-side (`SEND_GATE_SETUP.md`).

CLI:
    send_gate.py check --kind K --recipient R [--class owner|third_party|unknown] [--purpose P]   # dry, never consumes; exit 0 / 3
    send_gate.py grant --kind K --recipient R --why "<reason>" [--standing] [--expires-days N | --expires-at YYYY-MM-DD[THH:MM:SSZ]]
    send_gate.py grant --kind K --purpose P --standing --why "<reason>" [--expires-at ...]      # any recipient, labelled sends only
    send_gate.py list [--standing]                                                    # approved rows the gate would honour
    send_gate.py revoke <id> --why "<reason>"                                         # status -> revoked
    send_gate.py blast-radius [--days 7]                                              # the send ledger vs the store
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import paths  # noqa: E402
import pending_approvals  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402

#: The kinds a chokepoint may ask about — one per channel family. `email` covers Proton and Gmail
#: (the recipient is the address either way); `calendar` covers an event's attendees (create) or the
#: event id (delete); `slack` is the MCP tool's channel; `sms`/`call`/`discord` are the three
#: single-recipient channels, gated only on an explicit override.
KINDS = ("email", "slack", "calendar", "sms", "call", "discord")

EXIT_REFUSED = 3

#: Row fields a recipient may be read from, in the order tried. `to` is what the triage skills
#: write (`../state/README.md`); the rest are the ids the undecidable sites gate on.
_RECIPIENT_FIELDS = ("to", "cc", "bcc", "recipient", "recipients", "draftId", "draft_id",
                     "eventId", "event_id", "channel_id", "channel")

_STANDING_KEY = "standing"
#: A purpose-scoped standing row: `purpose` names the act, `to` is `PURPOSE_ANY`. Such
#: a row matches ONLY when the caller passes the same `purpose`; the recipient match skips it.
_PURPOSE_KEY = "purpose"
PURPOSE_ANY = "*"
_PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_expiry(text: str) -> str:
    """`--expires-at` in either shape the store already reads — a bare `YYYY-MM-DD` (midnight UTC at
    the start of that day) or a full `YYYY-MM-DDTHH:MM:SSZ` — returned as the full stamp. Anything
    else raises `ValueError` rather than storing an expiry `_expired` would read as already lapsed."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return _stamp(when)
    raise ValueError(f"--expires-at {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ (UTC)")


def normalize_recipients(recipient) -> list:
    """A recipient argument in any of the shapes a call site has — one string (possibly
    comma-separated), a list of them, or `None` — as a sorted, de-duplicated list of lowercase
    tokens. Blank entries vanish."""
    out = set()
    items = recipient if isinstance(recipient, (list, tuple, set)) else [recipient]
    for raw in items:
        if raw is None:
            continue
        for part in str(raw).split(","):
            tok = part.strip().lower()
            if tok:
                out.add(tok)
    return sorted(out)


def row_recipients(row: dict) -> set:
    """Every recipient token a store row names, across the fields the writers use — `to` for a
    triage draft, `draftId`/`eventId` for the id-gated sites, the channel part of a Slack
    `channelRef` (`slack:<channel>[:<ts>]` → `<channel>`)."""
    toks = set()
    for field in _RECIPIENT_FIELDS:
        toks.update(normalize_recipients(row.get(field)))
    ref = row.get("channelRef")
    if isinstance(ref, str) and ref.lower().startswith("slack:"):
        toks.add(ref.split(":", 2)[1].strip().lower())
    return toks


def _expired(row: dict, now: datetime) -> bool:
    exp = row.get("expires_at")
    if not exp:
        return False
    try:
        when = datetime.strptime(str(exp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # an unreadable expiry is an expired grant, never an open-ended one
    return now >= when


def _third_party_targets(kind: str, recipients: list) -> list:
    """The recipients that need covering. For the address-shaped kinds the owner's own addresses
    (`send_recipients.self_addresses`) are dropped — a cc to the owner on a third-party send needs
    no approval; for everything else every token is a target."""
    if kind in ("email", "calendar"):
        mine = send_recipients.self_addresses()
        return [r for r in recipients if r not in mine]
    return list(recipients)


def _verdict(allowed: bool, reason: str, *, kind: str, recipients: list, recipient_class: str,
             approval_id=None, approval_ids=None, error: str | None = None,
             purpose: str | None = None) -> dict:
    v = {"allowed": bool(allowed), "reason": reason, "kind": kind,
         "recipient_class": recipient_class, "recipients": recipients,
         "approval_id": approval_id}
    if approval_ids:
        v["approval_ids"] = list(approval_ids)
    if error:
        v["error"] = error
    if purpose:
        v["purpose"] = purpose
    return v


def evaluate(kind: str, recipient, *, recipient_class: str | None = None,
             state_dir: str | None = None, consume: bool = True, channel: str | None = None,
             now: datetime | None = None, purpose: str | None = None) -> dict:
    """The decision, un-wrapped — raises on a corrupt store or a bad kind so `require_approval` can
    turn that into a refusal. `consume=False` is the dry read (`check`). `purpose` is the caller's
    label for a purpose-scoped standing grant (never the sender's own claim — the hook derives it
    from the message through the purpose's detector); a labelled send that has no such grant falls
    through to the ordinary recipient match."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {KINDS}")
    if purpose is not None and not _PURPOSE_RE.match(str(purpose)):
        raise ValueError(f"bad purpose {purpose!r}: a lowercase kebab slug, 1-40 chars")
    recipients = normalize_recipients(recipient)
    if recipient_class is None:
        if kind in ("email", "calendar"):
            recipient_class = send_recipients.classify_emails(*recipients) if recipients else "unknown"
        else:
            recipient_class = "owner" if not recipients else "unknown"
    if recipient_class == "owner":
        return _verdict(True, "owner", kind=kind, recipients=recipients,
                        recipient_class=recipient_class)

    reference = now or datetime.now(timezone.utc)
    targets = _third_party_targets(kind, recipients)
    if not targets:
        # A non-owner class with nothing to match against (e.g. `unknown` with no token at all) —
        # nothing can be approved for it, so nothing passes.
        return _verdict(False, "no-recipient", kind=kind, recipients=recipients,
                        recipient_class=recipient_class,
                        error="the gate has no recipient token to match an approval against")

    doc = pending_approvals.load(state_dir)  # raises CorruptStore — a refusal, handled by the caller
    approved = [r for r in doc.get("approvals", [])
                if r.get("kind") == kind and r.get("status") == "approved"
                and not _expired(r, reference)]

    # 0. a purpose-scoped standing grant covers any recipient — of a send the caller labelled with
    #    that purpose, and nothing else. Never spent.
    if purpose:
        for r in approved:
            if r.get(_STANDING_KEY) and r.get(_PURPOSE_KEY) == purpose:
                return _verdict(True, "purpose", kind=kind, recipients=recipients,
                                recipient_class=recipient_class, approval_id=r.get("id"),
                                approval_ids=[r.get("id")], purpose=purpose)
    # a purpose row is not a recipient grant: it plays no part in the match below
    approved = [r for r in approved if not r.get(_PURPOSE_KEY)]

    # 1. standing grants cover what they cover, and are never spent
    standing = [r for r in approved if r.get(_STANDING_KEY)]
    covered_by = {}
    for r in standing:
        for tok in row_recipients(r) & set(targets):
            covered_by.setdefault(tok, r.get("id"))
    uncovered = [t for t in targets if t not in covered_by]
    if not uncovered:
        return _verdict(True, "standing", kind=kind, recipients=recipients,
                        recipient_class=recipient_class,
                        approval_id=covered_by[targets[0]],
                        approval_ids=sorted(set(covered_by.values())))

    # 2. one single-use approval whose recipients cover the rest, oldest first
    single = [r for r in approved if not r.get(_STANDING_KEY) and not r.get("gate_consumed_at")]
    single.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    for r in single:
        if set(uncovered) <= row_recipients(r):
            if consume:
                pending_approvals.resolve(
                    r["id"], "approved",
                    fields={"gate_consumed_at": _stamp(reference), "gate_channel": channel or kind},
                    state_dir=state_dir)
            return _verdict(True, "approved", kind=kind, recipients=recipients,
                            recipient_class=recipient_class, approval_id=r.get("id"),
                            approval_ids=sorted(set(covered_by.values()) | {r.get("id")}))

    return _verdict(False, "no-approval", kind=kind, recipients=recipients,
                    recipient_class=recipient_class,
                    error=f"no approved row in pending-approvals.json covers {uncovered} for kind "
                          f"{kind!r}")


def require_approval(kind: str, recipient, *, recipient_class: str | None = None,
                     state_dir: str | None = None, consume: bool = True,
                     channel: str | None = None, now: datetime | None = None,
                     purpose: str | None = None) -> dict:
    """THE gate. Returns a verdict dict — `allowed` decides; `reason` is one of `owner` /
    `purpose` / `standing` / `approved` (allowed) or `no-approval` / `no-recipient` /
    `store-corrupt` / `gate-error` (refused). Never raises: any exception is a refusal, because the
    one thing this module must never do is let a send through by accident."""
    try:
        return evaluate(kind, recipient, recipient_class=recipient_class, state_dir=state_dir,
                        consume=consume, channel=channel, now=now, purpose=purpose)
    except pending_approvals.CorruptStore as exc:
        return _verdict(False, "store-corrupt", kind=kind,
                        recipients=normalize_recipients(recipient),
                        recipient_class=recipient_class or "unknown", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — fail CLOSED
        return _verdict(False, "gate-error", kind=kind,
                        recipients=normalize_recipients(recipient),
                        recipient_class=recipient_class or "unknown",
                        error=f"{type(exc).__name__}: {exc}")


def refusal_line(verdict: dict) -> str:
    """The one clear line a refused send prints: what is missing and how to grant it — the
    single-use path (`send a<N>` → `pending_approvals.py resolve a<N> --status approved`, the
    Telegram picker path email/Slack triage already use) and the standing path (`send_gate.py
    grant --standing`, for a recipient the owner has pre-cleared)."""
    kind = verdict.get("kind")
    recips = ",".join(verdict.get("recipients") or []) or "<none>"
    why = verdict.get("error") or verdict.get("reason")
    return (f"send_gate REFUSED ({verdict.get('reason')}): {why}. Nothing was sent. To allow this "
            f"{kind} send to {recips}: approve the held draft (`pending_approvals.py resolve a<N> "
            f"--status approved` after the owner's `send a<N>`), or, for a recipient the owner "
            f"has pre-cleared, `send_gate.py grant --kind {kind} --recipient {recips} --standing "
            f"--why \"<the owner's decision>\"`. A one-off: the same `grant` without --standing.")


def refusal_payload(verdict: dict, **extra) -> dict:
    """The JSON a chokepoint prints on refusal — `ok: false`, `refused: "send_gate"`, the line, the
    verdict — so a caller matching on the shape reads it the same way from every site."""
    out = {"ok": False, "sent": False, "refused": "send_gate", "error": refusal_line(verdict),
           "gate": verdict}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- grants

def grant(kind: str, recipient=None, *, why: str, standing: bool = False,
          expires_days: int | None = None, expires_at: str | None = None, granted_by: str = "cli",
          purpose: str | None = None, state_dir: str | None = None,
          now: datetime | None = None) -> dict:
    """Mint an already-approved row through `pending_approvals.add` + `resolve` (still the one
    writer): `{kind, to, status: "approved", granted_by, granted_at, reason[, standing][,
    expires_at][, purpose]}`. `why` is required — a grant with no stated reason is refused, the same
    way an approval with no recorded ask is no approval at all. A `purpose` grant must be standing
    (it names an act, not a send) and takes `to: "*"` — a recipient on it would be meaningless, so
    one is refused rather than silently ignored."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {KINDS}")
    recipients = normalize_recipients(recipient)
    if purpose is not None:
        if not _PURPOSE_RE.match(str(purpose)):
            raise ValueError(f"bad --purpose {purpose!r}: a lowercase kebab slug, 1-40 chars")
        if not standing:
            raise ValueError("a --purpose grant must be --standing: it covers an act, not one send")
        if recipients and recipients != [PURPOSE_ANY]:
            raise ValueError("a --purpose grant covers any recipient — drop --recipient")
        recipients = [PURPOSE_ANY]
    if not recipients:
        raise ValueError("grant needs at least one recipient token")
    if not (why or "").strip():
        raise ValueError("grant needs --why: the reason (the owner's decision, the picker id, the message)")
    if expires_days is not None and expires_at:
        raise ValueError("--expires-days and --expires-at are alternatives, not both")
    reference = now or datetime.now(timezone.utc)
    fields = {"kind": kind, "to": ",".join(recipients), "summary": f"send-gate grant: {why.strip()}",
              "reason": why.strip(), "granted_by": granted_by, "granted_at": _stamp(reference)}
    if standing:
        fields[_STANDING_KEY] = True
    if purpose is not None:
        fields[_PURPOSE_KEY] = purpose
        fields["summary"] = f"send-gate purpose grant [{purpose}]: {why.strip()}"
    if expires_days is not None:
        fields["expires_at"] = _stamp(reference + timedelta(days=int(expires_days)))
    elif expires_at:
        fields["expires_at"] = parse_expiry(expires_at)
    entry = pending_approvals.add(fields, state_dir=state_dir, now=reference)
    return pending_approvals.resolve(entry["id"], "approved", state_dir=state_dir)


def revoke(entry_id: str, *, why: str, state_dir: str | None = None,
           now: datetime | None = None) -> dict:
    if not (why or "").strip():
        raise ValueError("revoke needs --why")
    return pending_approvals.resolve(entry_id, "revoked",
                                     fields={"revoked_at": _stamp(now), "revoked_reason": why.strip()},
                                     state_dir=state_dir)


def list_grants(*, standing_only: bool = False, state_dir: str | None = None,
                now: datetime | None = None) -> list:
    reference = now or datetime.now(timezone.utc)
    rows = pending_approvals.list_entries(status="approved", state_dir=state_dir)
    out = []
    for r in rows:
        if standing_only and not r.get(_STANDING_KEY):
            continue
        out.append({"id": r.get("id"), "kind": r.get("kind"), "recipients": sorted(row_recipients(r)),
                    "standing": bool(r.get(_STANDING_KEY)), "purpose": r.get(_PURPOSE_KEY),
                    "expired": _expired(r, reference), "expires_at": r.get("expires_at"),
                    "consumed_at": r.get("gate_consumed_at"), "reason": r.get("reason"),
                    "created_at": r.get("created_at")})
    return out


# --------------------------------------------------------------------------- blast radius

def blast_radius(*, days: int = 7, state_dir: str | None = None, now: datetime | None = None) -> dict:
    """What the send ledger says the gate would have done / did over the last `days`: per-channel
    counts by class, and for every non-owner row whether it carried a gate verdict or (a row written
    before the gate existed) would have been refused by class alone, against the approvals the store
    held at the time. The ledger carries no recipient, so "had an approval" for a verdict-less row
    can only be "an approved/sent
    row of that kind existed in the store" — reported as such, never as a match."""
    reference = now or datetime.now(timezone.utc)
    since = reference - timedelta(days=days)
    path = os.path.join(paths.state_dir(state_dir), send_recipients.FILENAME)
    rows = []
    if os.path.exists(path):
        for r in stateio.iter_jsonl(path):
            try:
                at = datetime.fromisoformat(str(r.get("at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if at >= since:
                rows.append(r)
    try:
        approvals = pending_approvals.load(state_dir).get("approvals", [])
    except pending_approvals.CorruptStore:
        approvals = []
    by_channel: dict = {}
    non_owner = []
    for r in rows:
        ch, cls = r.get("channel", "?"), r.get("recipient_class", "unknown")
        by_channel.setdefault(ch, {}).setdefault(cls, 0)
        by_channel[ch][cls] += 1
        if cls != "owner":
            gate = r.get("gate")
            kind = _channel_kind(ch)
            had = [a.get("id") for a in approvals
                   if a.get("kind") == kind and a.get("status") in ("approved", "sent")
                   and str(a.get("created_at") or "") <= str(r.get("at") or "")]
            non_owner.append({"at": r.get("at"), "channel": ch, "recipient_class": cls,
                              "gate": gate,
                              "would_block": (not gate["allowed"]) if gate else True,
                              "approved_rows_of_kind_at_the_time": had})
    return {"since": _stamp(since), "until": _stamp(reference), "rows": len(rows),
            "owner": sum(1 for r in rows if r.get("recipient_class") == "owner"),
            "non_owner": len(non_owner), "by_channel": by_channel, "non_owner_rows": non_owner}


def _channel_kind(channel: str) -> str:
    if channel.startswith(("proton", "gmail")):
        return "email"
    if channel.startswith("gcal"):
        return "calendar"
    if channel.startswith("push_sms"):
        return "sms"
    if channel.startswith("push_call"):
        return "call"
    if channel.startswith("discord"):
        return "discord"
    if "slack" in channel:
        return "slack"
    return channel


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The outbound approval gate over state/pending-approvals.json.")
    ap.add_argument("--state-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="dry decision — never consumes; exit 0 allowed / 3 refused")
    ck.add_argument("--kind", required=True, choices=KINDS)
    ck.add_argument("--recipient", required=True)
    ck.add_argument("--class", dest="recipient_class", default=None,
                    choices=("owner", "third_party", "unknown"))
    ck.add_argument("--purpose", default=None,
                    help="label the send with a purpose (what a hook purpose detector would derive)")

    gr = sub.add_parser("grant", help="mint an approved row (single-use, --standing, or --purpose)")
    gr.add_argument("--kind", required=True, choices=KINDS)
    gr.add_argument("--recipient", default=None, help="required unless --purpose")
    gr.add_argument("--why", required=True)
    gr.add_argument("--standing", action="store_true")
    gr.add_argument("--purpose", default=None,
                    help="a purpose-scoped standing grant: any recipient of --kind, for sends the "
                         "hook labels with this purpose only")
    gr.add_argument("--expires-days", type=int, default=None)
    gr.add_argument("--expires-at", default=None, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ (UTC)")
    gr.add_argument("--granted-by", default="cli")

    ls = sub.add_parser("list", help="approved rows the gate would honour")
    ls.add_argument("--standing", action="store_true")

    rv = sub.add_parser("revoke", help="status -> revoked")
    rv.add_argument("id")
    rv.add_argument("--why", required=True)

    br = sub.add_parser("blast-radius", help="the send ledger vs the store over the last N days")
    br.add_argument("--days", type=int, default=7)

    args = ap.parse_args(argv)
    sd = args.state_dir
    try:
        if args.cmd == "check":
            v = require_approval(args.kind, args.recipient, recipient_class=args.recipient_class,
                                 state_dir=sd, consume=False, purpose=args.purpose)
            print(json.dumps(v, ensure_ascii=False))
            if not v["allowed"]:
                print(refusal_line(v), file=sys.stderr)
                return EXIT_REFUSED
            return 0
        if args.cmd == "grant":
            if args.recipient is None and args.purpose is None:
                raise ValueError("grant needs --recipient (or --purpose for a purpose-scoped grant)")
            row = grant(args.kind, args.recipient, why=args.why, standing=args.standing,
                        expires_days=args.expires_days, expires_at=args.expires_at,
                        granted_by=args.granted_by, purpose=args.purpose, state_dir=sd)
            print(json.dumps(row, ensure_ascii=False))
            return 0
        if args.cmd == "list":
            print(json.dumps(list_grants(standing_only=args.standing, state_dir=sd), ensure_ascii=False))
            return 0
        if args.cmd == "revoke":
            print(json.dumps(revoke(args.id, why=args.why, state_dir=sd), ensure_ascii=False))
            return 0
        print(json.dumps(blast_radius(days=args.days, state_dir=sd), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, pending_approvals.CorruptStore, pending_approvals.NotFound) as exc:
        print(f"send_gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
