#!/usr/bin/env python3
"""The durable record of *which job completion pushes have already gone out*. Standard library only.

**Why this exists.** `jobs.py`'s stated non-negotiable is "every terminal state pushes the owner
exactly once", and a single field on the job record — `notified_at`, set by `reconcile` after a
landed send — is a correct gate against the ordinary case and a **useless** one against the case that
actually happens, because the job record has more than one writer. The race, with a cancel:

    t+0.0 s   `jobs.py cancel` (its own process)  claims the record: status=cancelled, ended_at,
                                                  cancelled_at. Saves.
    t+0.3 s   same process                        kills the child tree — each kill can take tens of
                                                  seconds to return.
    t+0.9 s   daemon, reconcile tick              terminal + un-notified → PUSH LANDS. `notified_at`
                                                  stamped and saved.
    t+~20 s   `jobs.py cancel`                    the kills return; the post-kill re-assert writes its
                                                  **stale in-memory record**. No `notified_at`.
    t+~24 s   daemon, next tick                   reads terminal + un-notified → **PUSHES AGAIN**,
                                                  and wakes the warm session a second time.

The owner gets the same message twice, and the record says `notified_at` exactly once — because the
first stamp was overwritten, not because only one send happened. Cancels are the exposed path (a
non-cancel terminal job has one writer at the end). The merge fix for the stale write is in
`jobs.cancel_job`; **this module is the other half**, and it is the half that does not depend on
enumerating the writers correctly.

**THE GATE IS IDENTITY, NOT RESEMBLANCE.** An entry is keyed by `(job_id, outcome)` — *this job, this
ending* — and nothing else. Deliberately **not** the push text: `notify_text` quotes the job log's
last line, and `kill_pid`'s own docstring records that a cancelled job's grandchildren can keep
appending to that log for minutes, so two pushes about the same ending can differ byte-for-byte. A
text-similarity gate would miss exactly the duplicates this was built for and would suppress genuinely
different messages the moment two jobs shared a title (two same-titled jobs cancelled minutes apart
each deserve their own push).

**IT DEDUPES; IT NEVER SUPPRESSES, AND EVERY DEGRADATION IS TOWARD SENDING.** The polarity here is the
opposite of most of this tree: elsewhere a lost message is the only harm, so those paths fail open
*into silence*. Here the harm is the duplicate — but a completion push that never arrives is still
worse, because a job that finished unreported is the exact failure `jobs.py` exists to prevent. So an
unreadable ledger, an unwritable directory, a wedged lock and a claim whose holder died all resolve
to **send anyway**. The only two states that stop a send are ones where a send provably or probably
already happened:

* `landed` — the send returned success. Never send this `(job, outcome)` again.
* `ambiguous` — the request went out and Telegram may have acted on it (a transport-level
  "maybe delivered" phase, or a `telegram_send.py` subprocess that timed out). Never send it again
  either: the send path already treats an ambiguous send as delivered, and re-litigating that one
  layer up is exactly how a duplicate gets reintroduced.
* `failed` — provably pre-delivery. Nothing was delivered, so the next tick sends, as it always has.

**A PREVENTED DUPLICATE IS LOUD.** `claim()` names its reason, `reconcile` logs it, and the entry
carries a `prevented` counter plus one `prevented_at` stamp per refusal — so "how often is this
firing?" is a query (`python job_push_ledger.py --stats`) and not an archaeology exercise. A dedupe
nobody can audit is how a silent-drop defect ships.

**Only ONE code path writes here** — `jobs.reconcile`'s push block — and that is the property that
makes it work where `notified_at` did not. `cancel_job` does not touch it. The shim does not touch
it. `honour_cancel` does not touch it. `prune` deletes an entry only alongside the job record it
belongs to.

Cross-process safety is a plain `O_CREAT|O_EXCL` lockfile, the same primitive and the same
steal-after-stale/fail-open semantics as `reminders_acks.queue_lock` — the only cross-process mutex
that is stdlib on Windows. The two processes that can genuinely race are the daemon's ~5 s reconcile
tick and a hand-run `python jobs.py reconcile`.

Runtime state: `state/job-pushes/<job-id>.json` (one entry per job) + `state/job-pushes.lock`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone

#: Directory under the state dir holding one entry per job that has been pushed about.
PUSH_DIR = "job-pushes"
#: The cross-process mutex guarding the read-modify-write of an entry.
LOCK_FILE = "job-pushes.lock"
SCHEMA = "seneschal.job-push/1"

#: The send landed — the API said yes.
LANDED = "landed"
#: The request went out; Telegram may or may not have acted on it. NOT re-sendable.
AMBIGUOUS = "ambiguous"
#: Provably nothing was delivered. Re-sendable, and re-sending is the guarantee.
FAILED = "failed"

#: The two outcomes that mean "the owner has probably already seen this", and therefore stop a re-send.
DELIVERED = (LANDED, AMBIGUOUS)

#: How long an unresolved claim is believed to belong to a live sender before it is taken over.
#: Comfortably past the send path's own ceilings: `sentinel.send_telegram` caps the `telegram_send.py`
#: subprocess at 60 s, and a reply path that retries once after a short sleep roughly doubles that —
#: ~2 min worst case. Past this the holder is assumed dead and we send rather than stay silent.
IN_FLIGHT_STALE_SEC = 180

#: Lock acquisition budget. Short: past it we proceed unlocked, which degrades to the pre-lock race
#: (a possible duplicate) rather than to a missing push.
LOCK_TIMEOUT_SEC = 5.0
#: A lockfile older than this had its holder die mid-section; steal it.
LOCK_STALE_SEC = 60.0


def _stamp(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def push_dir(state_dir: str) -> str:
    return os.path.join(state_dir, PUSH_DIR)


def entry_path(state_dir: str, job_id: str) -> str:
    return os.path.join(push_dir(state_dir), f"{job_id}.json")


@contextlib.contextmanager
def _lock(state_dir: str, timeout: float = LOCK_TIMEOUT_SEC, stale_sec: float = LOCK_STALE_SEC):
    """Cross-process advisory mutex over the ledger. **Fail-open past `timeout`** — see the module
    docstring: an unavailable lock must degrade toward sending, never toward silence."""
    path = os.path.join(state_dir, LOCK_FILE)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(path) > stale_sec:
                    os.remove(path)   # the holder died mid-section — steal and retry immediately
                    continue
            if time.monotonic() >= deadline:
                break                 # fail-open
            time.sleep(0.02)
        except OSError:
            break                     # can't even create the lock — fail-open
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(path)


def read(state_dir: str, job_id: str) -> dict | None:
    """One entry, or None if absent/unreadable. **Never raises** — this is consulted on the daemon's
    ~5 s tick, and an unreadable entry must read as "no record of a push", which sends."""
    try:
        with open(entry_path(state_dir, job_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("job_id") else None


def _write(state_dir: str, entry: dict) -> bool:
    """Atomic-ish write of one entry. Returns whether it landed; a False here never stops a send."""
    path = entry_path(state_dir, entry["job_id"])
    tmp = path + ".tmp"
    try:
        os.makedirs(push_dir(state_dir), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False


def _last_attempt(entry: dict) -> dict:
    attempts = entry.get("attempts")
    return attempts[-1] if isinstance(attempts, list) and attempts else {}


def delivery_of(entry: dict | None) -> str | None:
    """The most recent RESOLVED delivery for this entry, or None if there is none yet.

    Scans backwards rather than reading only the last attempt: a takeover appends a fresh unresolved
    attempt on top, and a prior `landed` underneath it is still the truth about what the owner saw."""
    if not isinstance(entry, dict):
        return None
    for att in reversed(entry.get("attempts") or []):
        if isinstance(att, dict) and att.get("delivery") in (LANDED, AMBIGUOUS, FAILED):
            if att["delivery"] in DELIVERED:
                return att["delivery"]
    for att in reversed(entry.get("attempts") or []):
        if isinstance(att, dict) and att.get("delivery"):
            return att["delivery"]
    return None


def delivered_at(entry: dict | None) -> str | None:
    """When the delivered (landed/ambiguous) attempt resolved — what a caller stamps `notified_at`
    from, so a record whose stamp was clobbered gets the ORIGINAL instant back rather than now."""
    if not isinstance(entry, dict):
        return None
    for att in reversed(entry.get("attempts") or []):
        if isinstance(att, dict) and att.get("delivery") in DELIVERED:
            return att.get("resolved_at") or att.get("at")
    return None


def claim(state_dir: str, job_id: str, outcome: str, *, now: datetime | None = None,
          stale_sec: float = IN_FLIGHT_STALE_SEC, pid: int | None = None) -> dict:
    """Ask permission to push about `(job_id, outcome)`, and record the attempt if granted.

    Returns ``{granted, reason, delivery, delivered_at, attempt, prevented}``. **`granted=False` is
    the only thing that stops a send**, and it happens in exactly three cases:

      * ``already-delivered`` — a prior attempt landed. The owner has it.
      * ``already-ambiguous`` — a prior attempt went out and may have landed. Re-sending is the
        duplicate; the send path already treats an ambiguous send as delivered.
      * ``in-flight-elsewhere`` — another process claimed it less than `stale_sec` ago and has not
        reported back. Not a refusal so much as a *wait*: the caller simply retries next tick, and if
        that sender died the claim goes stale and is taken over.

    Granted reasons, all of which are worth a log line: ``first-attempt``,
    ``retry-after-failed-send`` (the prior attempt was provably pre-delivery),
    ``stale-claim-taken-over`` (a sender died mid-send — we accept a possible duplicate rather than
    risk silence, and say so), and ``outcome-changed`` (the record's terminal state is not the one we
    pushed about; that is a different ending and deserves its own message).

    **Every error path grants.** A ledger that cannot be read or written must not be able to silence
    a completion push."""
    stamp = _stamp(now)
    with _lock(state_dir):
        entry = read(state_dir, job_id)
        if entry is not None and entry.get("outcome") != outcome:
            # A different ending than the one we already spoke about. Start a fresh identity rather
            # than reusing the old one's history — "this job, THIS outcome".
            entry = None
            reason = "outcome-changed"
        elif entry is None:
            reason = "first-attempt"
        else:
            delivery = delivery_of(entry)
            if delivery in DELIVERED:
                entry["prevented"] = int(entry.get("prevented") or 0) + 1
                entry["prevented_at"] = stamp
                _write(state_dir, entry)
                return {"granted": False,
                        "reason": "already-delivered" if delivery == LANDED else "already-ambiguous",
                        "delivery": delivery, "delivered_at": delivered_at(entry),
                        "attempt": len(entry.get("attempts") or []),
                        "prevented": entry["prevented"]}
            last = _last_attempt(entry)
            if not last.get("delivery"):
                age = None
                started = _parse(last.get("at"))
                if started is not None:
                    age = ((now or datetime.now(timezone.utc)) - started).total_seconds()
                if age is not None and age < stale_sec:
                    entry["prevented"] = int(entry.get("prevented") or 0) + 1
                    entry["prevented_at"] = stamp
                    _write(state_dir, entry)
                    return {"granted": False, "reason": "in-flight-elsewhere", "delivery": None,
                            "delivered_at": None, "attempt": len(entry.get("attempts") or []),
                            "prevented": entry["prevented"]}
                reason = "stale-claim-taken-over"
            else:
                reason = "retry-after-failed-send"

        if entry is None:
            entry = {"schema": SCHEMA, "job_id": job_id, "outcome": outcome, "attempts": [],
                     "prevented": 0}
        entry["outcome"] = outcome
        attempts = entry.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        attempts.append({"at": stamp, "pid": pid if pid is not None else os.getpid(),
                         "delivery": None, "resolved_at": None, "reason": reason})
        entry["attempts"] = attempts
        _write(state_dir, entry)
        return {"granted": True, "reason": reason, "delivery": None, "delivered_at": None,
                "attempt": len(attempts), "prevented": int(entry.get("prevented") or 0)}


def resolve(state_dir: str, job_id: str, delivery: str, *, now: datetime | None = None) -> None:
    """Record how the claimed attempt turned out (`landed` / `ambiguous` / `failed`). Never raises.

    An unresolved attempt is not a lost one — it goes stale after `IN_FLIGHT_STALE_SEC` and the next
    claim takes it over and sends. That is the deliberate degradation: a daemon that dies between the
    send and this call costs a possible duplicate, and the alternative costs the push entirely."""
    if delivery not in (LANDED, AMBIGUOUS, FAILED):
        delivery = FAILED
    stamp = _stamp(now)
    with _lock(state_dir):
        entry = read(state_dir, job_id)
        if entry is None:
            return
        attempts = entry.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return
        for att in reversed(attempts):
            if isinstance(att, dict) and not att.get("delivery"):
                att["delivery"] = delivery
                att["resolved_at"] = stamp
                break
        else:
            return
        _write(state_dir, entry)


def forget(state_dir: str, job_id: str) -> None:
    """Drop one entry — called by `jobs.prune` alongside the job record it belongs to, so the ledger
    has exactly the retention of the thing it describes. Never raises."""
    with contextlib.suppress(OSError):
        os.remove(entry_path(state_dir, job_id))


def entries(state_dir: str) -> list:
    """Every readable entry, oldest id first. A stray or malformed file is skipped silently."""
    out = []
    try:
        names = sorted(os.listdir(push_dir(state_dir)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = read(state_dir, name[: -len(".json")])
        if rec is not None:
            out.append(rec)
    return out


def stats(state_dir: str) -> dict:
    """The auditable summary: how many pushes the ledger has seen, how they resolved, and **how many
    duplicates it prevented**. This is the counter that makes the mechanism checkable — without it a
    dedupe is indistinguishable from a message that was never sent."""
    rows = entries(state_dir)
    out = {"entries": len(rows), "prevented": 0, "landed": 0, "ambiguous": 0, "failed": 0,
           "unresolved": 0, "retried_after_failure": 0, "taken_over": 0}
    for rec in rows:
        out["prevented"] += int(rec.get("prevented") or 0)
        for att in rec.get("attempts") or []:
            if not isinstance(att, dict):
                continue
            d = att.get("delivery")
            if d in (LANDED, AMBIGUOUS, FAILED):
                out[d] += 1
            else:
                out["unresolved"] += 1
            if att.get("reason") == "retry-after-failed-send":
                out["retried_after_failure"] += 1
            elif att.get("reason") == "stale-claim-taken-over":
                out["taken_over"] += 1
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Inspect the job completion-push ledger (read-only).")
    p.add_argument("--state-dir",
                   default=os.path.join(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))), "state"))
    p.add_argument("--stats", action="store_true", help="summary counters, incl. duplicates prevented")
    p.add_argument("--job", help="dump one entry")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    if args.job:
        rec = read(args.state_dir, args.job)
        if rec is None:
            print(f"no push ledger entry for {args.job}", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0

    data = stats(args.state_dir)
    if args.json or not args.stats:
        print(json.dumps(data, indent=2))
        return 0
    print(f"entries              {data['entries']}")
    print(f"duplicates PREVENTED {data['prevented']}")
    print(f"  landed             {data['landed']}")
    print(f"  ambiguous          {data['ambiguous']}")
    print(f"  failed (re-sent)   {data['failed']}")
    print(f"  unresolved         {data['unresolved']}")
    print(f"  retried after fail {data['retried_after_failure']}")
    print(f"  stale claims taken {data['taken_over']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
