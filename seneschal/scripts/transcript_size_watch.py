#!/usr/bin/env python3
"""The transcript archive's size sensor — raise it ONCE at the threshold. Standard library only.

The store it watches is `transcript_archive.py`; read that module's docstring first.

## Why this exists, and it is not a nicety

Retention for `state/transcripts/` is keep-everything — `RETENTION_DAYS = 0`, nothing prunes, and
**this module deletes nothing and never will**. It is a thermometer, not a thermostat.

**What it guards is the other half of that decision:** keep everything *until it gets big* — the
owner wants to hear when the archive passes ~200 MB. A threshold nothing measures is not a plan, it
is a promise with no ledger behind it: an escalation gated on a metric nobody built stays silent
through exactly the growth it was meant to catch (`../docs/notion-write-behind-outbox-spec.md` §7 is
that failure shape). So the decision and the sensor for it ship together, on purpose.

## Fire ONCE — the other half of the design

A threshold that re-nags every night is one the owner mutes, and a muted alarm is worse than no
alarm: it trains deafness. So a fired alert writes `state/transcript-size-alert.json` and the sensor
goes quiet.

**The marker records the threshold it fired at**, which is what makes *"until the number changes"*
mechanical rather than remembered: change `THRESHOLD_BYTES` (or pass `--threshold-bytes`) and the old
marker no longer matches, so the sensor re-arms by itself. Deleting the marker re-arms it too.

**The marker is written only AFTER the send has landed** — `jobs.py`'s rule, and it matters more here
than there, because there is exactly one alert to spend. A failed push that still burned the one-shot
would leave the archive over the threshold with the sensor permanently silent, which is the failure
this module exists to prevent, arrived at from the other side.

## Report the rate, not just the breach

*"It crossed 200 MB"* says nothing about whether that is a curiosity or next month's problem. So the
message carries the **observed** growth — bytes/day computed from what is on disk, never a constant.
(A typical archive grows on the order of a few hundred KB/day, which puts the threshold years out;
that figure is in this sentence and deliberately nowhere in the code.)

The rate is measured over the archive's **whole** days — every day file except the newest, which is
today's and still being written. Including a partial day drags the observed rate *below* the truth,
and an under-stated rate over-states how long there is, which is the wrong direction for a number
whose whole job is to say *when*. Fewer than two day files ⇒ **no rate is reported at all** rather
than one extrapolated from a single partial day. At the threshold there are years of files, so the
abstention can never bite at fire time — it is there so `status` cannot lie on a fresh install.

## Never cost a Dream run

Every entry point is wrapped and returns a verdict dict; nothing here raises into its caller. An
absent directory, an unreadable file, a missing Telegram config — each is a quiet `fired: False` and
exit 0, the same fail-open contract every other observer in this directory holds. A sensor that can
break the nightly run it rides on is a worse bug than the growth it watches.

The Mouth (`mouth.py`) records the alert as something the assistant said, when it is installed; it
is imported optionally, and its absence costs the record, never the alert.

USAGE:
  python transcript_size_watch.py status          # size + rate; NEVER sends, NEVER marks
  python transcript_size_watch.py check           # the Dream call: raise it once if over
  python transcript_size_watch.py check --dry-run # compute + show the message; no send, no marker
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone

import stateio
import transcript_archive

try:  # the Mouth's assertion record (optional — absent on an install without it)
    import mouth
except ImportError:
    mouth = None

# The size the owner wants to hear about — "about 200 MB". The binary interpretation (MiB) is this
# module's, and the difference is far below the precision of a round number. Change this constant (or
# pass `--threshold-bytes`) and the sensor re-arms itself (see the marker, above), which is the
# mechanism that makes a new number take effect without anyone remembering to delete a file.
THRESHOLD_BYTES = 200 * 1024 * 1024

# The horizon the alert quotes once the archive is already over: "the next 100 MB is about N days out".
# Not a second threshold — nothing fires on it, and nothing is deleted at it.
PROJECTION_STEP_BYTES = 100 * 1024 * 1024

MARKER_FILE = "transcript-size-alert.json"
SCHEMA = "seneschal.transcript-size-alert/1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = transcript_archive.DEFAULT_STATE_DIR
DEFAULT_TELEGRAM_ENV = os.path.join(SCRIPT_DIR, "telegram.env")


# ────────────────────────────────────────────────────────────────────────────────────── measuring

def _dir_bytes(path: str) -> tuple:
    """`(total_bytes, file_count)` for one directory, **without reading a byte of content**.

    `transcript_archive.stats()` answers a richer question and parses every row to do it; at 200 MB
    that is minutes of work for a number `st_size` already knows. This runs nightly, so it stays a
    stat sweep. Fail-open: an absent or unreadable directory measures as empty."""
    total = count = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                        count += 1
                except OSError:
                    continue  # one unreadable entry costs its own bytes, never the sweep
    except OSError:
        return 0, 0
    return total, count


def _day_span(first: str, last: str) -> int | None:
    """Calendar days from `first` to `last` inclusive — the denominator of the rate.

    **Calendar span, not the number of files.** A week the daemon was down leaves no day files for
    that week; dividing by the file count would quietly report the rate as if those days had not
    happened and over-state growth. What the archive actually grew by, per day that elapsed, is the
    honest figure."""
    try:
        return (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    except ValueError:
        return None


def measure(state_dir: str | None = None, *, threshold: int = THRESHOLD_BYTES) -> dict:
    """Size, span and observed growth of `state/transcripts/`. Never raises; never writes.

    `bytes_per_day` is `None` when the archive cannot support a rate (fewer than two day files, or
    unparseable filenames) — see the module docstring on why abstaining beats extrapolating."""
    archive = transcript_archive.archive_dir(state_dir)
    total, files = _dir_bytes(archive)
    days = transcript_archive.available_days(state_dir)

    rate = span = None
    if len(days) >= 2:
        whole = days[:-1]  # every day but today's, which is still being appended to
        span = _day_span(whole[0], whole[-1])
        if span and span > 0:
            whole_bytes = 0
            for day in whole:
                try:
                    whole_bytes += os.path.getsize(transcript_archive.archive_path(state_dir, day))
                except OSError:
                    continue
            rate = whole_bytes / span

    return {
        "dir": archive,
        "bytes": total,
        "files": files,
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "rate_over_days": span,
        "bytes_per_day": int(rate) if rate else None,
        "bytes_per_year": int(rate * 365) if rate else None,
        "threshold_bytes": threshold,
        "over_threshold": total >= threshold,
        "days_to_threshold": _days_until(total, rate, threshold),
        "days_to_next_step": _days_until(total, rate, total + PROJECTION_STEP_BYTES),
    }


def _days_until(current: int, rate: float | None, target: int) -> int | None:
    """Days at `rate` until the archive reaches `target`. `None` when unknowable or already past."""
    if not rate or rate <= 0 or current >= target:
        return None
    return int(math.ceil((target - current) / rate))


# ────────────────────────────────────────────────────────────────────────────────────── the marker

def marker_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, MARKER_FILE)


def read_marker(state_dir: str | None = None) -> dict | None:
    """The record of the one alert this sensor has spent, or `None`. Fail-open: an unreadable or
    corrupt marker reads as absent, which re-arms the sensor — a duplicate alert costs a buzz, a
    suppressed one costs the whole feature."""
    try:
        with open(marker_path(state_dir), encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def already_fired(state_dir: str | None = None, *, threshold: int = THRESHOLD_BYTES) -> bool:
    """True iff an alert has been sent **for this threshold**.

    Keying on the number is what implements *"stay quiet until the number changes"* in code: a new
    `THRESHOLD_BYTES` is a new decision, and a marker from the old one must not silence it."""
    marker = read_marker(state_dir)
    return bool(marker) and marker.get("threshold_bytes") == threshold


def _write_marker(state_dir: str | None, row: dict) -> bool:
    """Build-then-`os.replace` via `stateio.write_json_atomic`, per this directory's house rule —
    never a truncate-write of a `state/` file, which are gitignored and exist nowhere else."""
    path = marker_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stateio.write_json_atomic(path, row)
        return True
    except Exception:  # noqa: BLE001 — see the module docstring: never cost the Dream run
        return False


# ───────────────────────────────────────────────────────────────────────────────────── the message

def human_bytes(value) -> str:
    """`209715200` → `"200 MB"`. Binary units, one decimal, trailing `.0` dropped."""
    if not isinstance(value, (int, float)):
        return "unknown"
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if abs(value) >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{int(value)} B"


def format_alert(m: dict) -> str:
    """The message the owner actually receives. Every number in it comes from `m` — the growth figure
    is measured, never quoted from the day this was written."""
    span = ""
    if m.get("first_day") and m.get("last_day"):
        span = f" ({m['first_day']} → {m['last_day']})"
    lines = [
        f"Heads up — `state/transcripts/` has crossed {human_bytes(m['threshold_bytes'])}. "
        f"It's at {human_bytes(m['bytes'])} now, across {m['days']} day files{span}.",
    ]
    if m.get("bytes_per_day"):
        pace = (f"It's growing about {human_bytes(m['bytes_per_day'])}/day "
                f"(~{human_bytes(m['bytes_per_year'])}/year)")
        if m.get("days_to_next_step"):
            pace += f", so the next {human_bytes(PROJECTION_STEP_BYTES)} is about " \
                    f"{m['days_to_next_step']} days out"
        lines.append(pace + ".")
    else:
        # Only reachable on an archive too short to support a rate — impossible at the threshold, but the
        # message must still be true rather than silently claiming a growth of zero.
        lines.append("I can't measure a growth rate yet — there aren't enough whole days on disk.")
    lines.append(
        "Nothing has been deleted and nothing will be: retention there is keep-everything. This is "
        "the one-time size check for the archive — I'll stay quiet about it from here unless the "
        "threshold changes."
    )
    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────────────── the alert

def _default_sender(text: str) -> dict:
    """Push through the sibling `telegram_send.py`, reusing `sentinel.send_telegram` rather than
    re-implementing the subprocess call. Auto-detects and degrades to absent, like every other
    optional integration here: with no `telegram.env` nothing is sent, **nothing is marked**, and the
    sensor simply tries again tomorrow."""
    if not os.path.exists(DEFAULT_TELEGRAM_ENV):
        return {"ok": False, "error": "telegram not configured (no scripts/telegram.env)"}
    try:
        from sentinel import send_telegram  # local import: keeps sentinel off this module's import cost
        return send_telegram(text, DEFAULT_TELEGRAM_ENV)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"send failed: {exc}"}


def _record_said(state_dir: str | None, text: str) -> None:
    """Log the alert to the Mouth — what the assistant has actually said, after a landed send.
    Best-effort and deliberately silent: `mouth.record_assertion` never raises, and an absent Mouth
    must not unspend an alert that already reached the owner."""
    if mouth is None:
        return
    try:
        mouth.record_assertion(state_dir or DEFAULT_STATE_DIR, surface="telegram", kind="alert",
                               speaker="dream", text=text)
    except Exception:  # noqa: BLE001
        pass


def check(state_dir: str | None = None, *, threshold: int = THRESHOLD_BYTES,
          sender=None, dry_run: bool = False, now: datetime | None = None) -> dict:
    """The nightly call. Measures, and raises the alert **once** if the archive is over `threshold`.

    Returns a verdict dict and **never raises** — the whole body is guarded, because this rides on a
    Dream run it is not allowed to cost. `fired` is True only on the run that actually delivered.
    """
    try:
        m = measure(state_dir, threshold=threshold)
        verdict = dict(m, ok=True, fired=False, breached=m["over_threshold"])

        if not m["over_threshold"]:
            return verdict
        if already_fired(state_dir, threshold=threshold):
            marker = read_marker(state_dir) or {}
            verdict["already_fired_at"] = marker.get("fired_at")
            return verdict

        message = format_alert(m)
        verdict["message"] = message
        if dry_run:
            verdict["dry_run"] = True
            return verdict

        result = (sender or _default_sender)(message)
        if not (isinstance(result, dict) and result.get("ok")):
            # NOT marked — the one-shot is spent on delivery, not on the attempt. Tomorrow retries.
            verdict["send_error"] = result.get("error", "send failed") \
                if isinstance(result, dict) else "sender returned no result"
            return verdict

        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict["fired"] = True
        verdict["fired_at"] = stamp
        verdict["marked"] = _write_marker(state_dir, {
            "schema": SCHEMA,
            "fired_at": stamp,
            "threshold_bytes": threshold,
            "bytes": m["bytes"],
            "bytes_per_day": m["bytes_per_day"],
            "days": m["days"],
            "message": message,
        })
        _record_said(state_dir, message)
        return verdict
    except Exception as exc:  # noqa: BLE001 — a sensor may never be what breaks the run it rides on
        return {"ok": False, "fired": False, "breached": False, "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────────────────── CLI

def main(argv=None, sender=None) -> int:
    """`sender` is a TEST SEAM, not a flag: it defaults to `None` so the console path
    `sys.exit(main())` is byte-identical, and it exists so no test can ever message the owner for real
    (`presence.py`'s offline-test lesson). Nothing in the tree passes it."""
    p = argparse.ArgumentParser(description="Size sensor for the durable transcript archive.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--threshold-bytes", type=int, default=THRESHOLD_BYTES,
                   help="override the threshold (a different number re-arms the sensor)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="size + observed growth; never sends, never marks")
    chk = sub.add_parser("check", help="raise it once if over the threshold")
    chk.add_argument("--dry-run", action="store_true", help="show the message; no send, no marker")

    args = p.parse_args(argv)

    if args.cmd == "status":
        out = measure(args.state_dir, threshold=args.threshold_bytes)
        out["already_fired"] = already_fired(args.state_dir, threshold=args.threshold_bytes)
        out["human"] = {
            "bytes": human_bytes(out["bytes"]),
            "per_day": human_bytes(out["bytes_per_day"]) if out["bytes_per_day"] else None,
            "threshold": human_bytes(out["threshold_bytes"]),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    verdict = check(args.state_dir, threshold=args.threshold_bytes,
                    sender=sender, dry_run=args.dry_run)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0  # always: the sensor never fails the run it rides on


if __name__ == "__main__":
    sys.exit(main())
