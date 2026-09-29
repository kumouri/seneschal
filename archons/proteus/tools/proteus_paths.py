#!/usr/bin/env python3
"""Canonical Proteus paths — the ``state/`` vs ``out/`` split, plus legacy migration.

**The Archon layout rule** (applies to every Archon — see
``seneschal/references/archons.md``; enforced at mint time by ``subagents/archon-forge``):

``<archon>/state/``
    **Runtime churn.** Perpetually rewritten, regenerable, never in git: rolling
    ledgers, "latest" caches, counters, queues, sentinels, pid/log files. If a run
    rewrites it every cycle, it belongs here.

``<archon>/out/``
    **Deliverables.** The artifacts a run *produces* for the owner — drafted documents,
    digests, generated views. Also gitignored here (Proteus's outputs embed the owner's
    contact details), but these are products, not machinery.

``<archon>/`` (tracked)
    Curated inputs + code: profile, watchlist, voice profile, ``tools/``, ``.gitignore``.

Every archon carries its **own** ``.gitignore`` so the split holds wherever the archon
is checked out — not only inside this repo's tree, where the root ignore file happens
to cover it.

This module is the single source of truth for those paths so the tools
(``hunt_cycle``, ``daily_digest``, ``fetch_jobs``, ``score_jobs``, ``record_intel``,
``promote_intel``, ``wttj_discover``) can't drift apart.

It also owns **posting identity** (``norm_url``) and the **atomic writer** the tools share, for the
same reason: ``hunt_cycle``'s alert and ``daily_digest``'s email both decide whether an apply link
is "the same link" as the posting, and every tool that rewrites a ``state/`` file must do it the
same crash-safe way.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROTEUS_DIR = TOOLS_DIR.parent                    # archons/proteus
STATE_DIR = PROTEUS_DIR / "state"                 # runtime churn (gitignored)
OUT_DIR = PROTEUS_DIR / "out"                     # deliverables (gitignored)
STABLE_DIR = PROTEUS_DIR.parent / "stable" / PROTEUS_DIR.name   # archons/stable/proteus (TRACKED)

# --- state/ : rewritten every cycle -------------------------------------------
SEEN_FILE = STATE_DIR / "seen.json"               # rolling dedup ledger (do NOT lose)
CYCLES_FILE = STATE_DIR / "cycles.jsonl"          # one line per hunt cycle
JOBS_LATEST = STATE_DIR / "jobs-latest.json"      # last fetch (large, overwritten)
SCORED_LATEST = STATE_DIR / "scored-latest.json"  # last score (large, overwritten)
PAUSED_FILE = STATE_DIR / "paused"                # sentinel: pause the hourly hunt
LOGS_DIR = STATE_DIR / "logs"                     # deploy/delegate logs + pid files
RUNS_DIR = STATE_DIR / "runs"                     # per-run raw boards: runs/<run-date>/{jobs,scored}.json
LEDGER_FILE = STATE_DIR / "ledger.jsonl"          # demiurge's delegation ledger — see below
INTEL_PENDING_FILE = STATE_DIR / "company-intel-pending.jsonl"  # proposed intel awaiting promotion
WTTJ_DISCOVERY_FILE = STATE_DIR / "wttj-discovery.json"         # wttj_discover.py's last report

# --- the atomic writer, re-exported so the tools share ONE ------------------------
# `seneschal/scripts/memory_write.py` is the repo's atomic write, and there is deliberately no second
# implementation of it. It lives here for the same reason the paths do: several tools writing
# `state/` several ways is the drift this module exists to prevent, and `seen.json` (the dedup
# ledger) has no copy anywhere — a torn write re-alerts every open role.
_SCRIPTS_DIR = PROTEUS_DIR.parent.parent / "seneschal" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import memory_write as _memory_write  # noqa: E402

#: Atomically replace `path`'s text: sibling temp file, fsync, `os.replace`. On any failure the
#: target is untouched, because it was never opened. Line endings and a BOM are preserved.
write_text = _memory_write.write_text


def write_json(path, obj, **dumps_kwargs) -> None:
    """`write_text` for a JSON payload. `ensure_ascii=False` by default: these files hold company and
    role names, and escaping them to `\\uXXXX` makes the ledger unreadable to a human eye."""
    dumps_kwargs.setdefault("ensure_ascii", False)
    write_text(str(path), json.dumps(obj, **dumps_kwargs))


def owner_today(now: datetime | None = None) -> str:
    """The owner's local calendar date (``YYYY-MM-DD``) at ``now`` (default: the wall clock).

    Resolved through ``seneschal/scripts/tz_common.py`` — the owner's configured zone, with its
    documented fallback ladder — so an intel stamp's ``updated`` date agrees with every other date
    the suite writes. Never raises: a host where the zone can't be resolved stamps the UTC date
    rather than failing a recording. ``now`` is injectable so tests pin the instant.
    """
    instant = now or datetime.now(timezone.utc)
    try:
        import tz_common
        return tz_common.to_local(instant).date().isoformat()
    except Exception:  # noqa: BLE001 — a date stamp must never cost the write it labels
        return instant.astimezone(timezone.utc).date().isoformat()


# --- out/ : what a run produces -----------------------------------------------
DIGEST_DIR = OUT_DIR / "digests"                  # daily digest markdown
MANIFEST_FILE = OUT_DIR / "workups.json"          # index of drafted workups

# --- curated inputs --------------------------------------------------------------
PROFILE_FILE = PROTEUS_DIR / "profile.json"
WATCHLIST_FILE = PROTEUS_DIR / "watchlist.json"
# Proteus's durable company memory — a curated input the archon CONTRIBUTES to, so it is
# deliberately NOT under state/. It is the owner's data and GITIGNORED (like profile.json). Proteus
# never writes it directly: it appends proposals to INTEL_PENDING_FILE above (`record_intel.py`), and
# `promote_intel.py` merges them here — locally by default, or by PR (`--via-pr`) for an owner who
# keeps it in a private fork. See `seneschal/references/archons.md`.
COMPANY_INTEL_FILE = PROTEUS_DIR / "company-intel.json"

# Pre-state/ layout: runtime files used to sit in out/hourly/ and out/ alongside the
# deliverables. (new, old) pairs — kept so an existing checkout self-heals instead of
# regenerating a multi-MB seen-ledger from scratch and re-alerting every posting.
_LEGACY_MOVES: tuple[tuple[Path, Path], ...] = (
    (OUT_DIR / "hourly" / "seen.json", SEEN_FILE),
    (OUT_DIR / "hourly" / "cycles.jsonl", CYCLES_FILE),
    (OUT_DIR / "hourly" / "jobs-latest.json", JOBS_LATEST),
    (OUT_DIR / "hourly" / "scored-latest.json", SCORED_LATEST),
    (OUT_DIR / "hourly" / "paused", PAUSED_FILE),
    # The delegation ledger, out of the TRACKED stable (a standing layout decision). It was the one
    # part of stable/ that churned — demiurge appends on every delegation — which meant (a) the live
    # checkout was permanently dirty, arming the `pull --ff-only` failure that silently stops
    # merge-is-deploy reloads, and (b) the full request/response text of every delegation went into
    # git history. That text is operational context by nature; history can't be de-personalized by
    # editing a worktree. Fixed upstream in demiurge (the `--ledger-dir` flag) — the orchestrator
    # passes it on every delegate/verdict/distill/tenure (crib: seneschal/references/archons.md).
    (STABLE_DIR / "ledger.jsonl", LEDGER_FILE),
)

_LEGACY_LOG_GLOBS = ("delegate-*.log", "delegate-*.log.err", "delegate-task.txt",
                     "deploy*.log", "deploy*.log.err", "deploy.pid", "run-delegation.ps1")


# Raw pipeline boards, wherever a caller points them. These are machinery: a full scored dump of every
# posting (many MB each), regenerable from a re-fetch, and of no use to the owner. The charter's
# Phase 1 hardcodes `--out .../out/<run-date>/jobs.json`, so every delegated run quietly filled out/
# with them — raw boards sitting next to the resumes and cover letters they were supposed to produce.
_RAW_ARTIFACTS = frozenset({"jobs.json", "scored.json"})


def resolve_raw_artifact(path) -> Path:
    """Route a raw board (``jobs.json`` / ``scored.json``) aimed at ``out/`` into ``state/runs/`` instead.

    Anything else — a resume, a cover letter, a digest, a path already under ``state/`` — is returned
    untouched. ``out/<run-date>/jobs.json`` becomes ``state/runs/<run-date>/jobs.json``.

    **Why redirect rather than just change the default:** the charter is fixed at mint time and is the
    scope authority, so its literal Phase-1 commands must keep working end-to-end. Both the writer
    (``fetch_jobs --out``) and the reader (``score_jobs --jobs``) call this, so they agree on where the
    file went and the pipeline still chains. Revising the charter's prose to match is a
    ``demiurge revise`` — ask-high, the owner's call — and this holds the line until then.
    """
    path = Path(path)
    if path.name not in _RAW_ARTIFACTS:
        return path
    try:
        relative = path.resolve().relative_to(OUT_DIR.resolve())
    except (ValueError, OSError):
        return path                      # not under out/ — the caller already knows what it wants
    return RUNS_DIR / relative


# How long a per-run raw board is worth keeping. Nothing READS state/runs/ — it exists so a work-up can
# be reproduced ("why did you score this 84%?"), which is only worth anything while the work-up is still
# under review. 7 days, not 24 h, because the owner doesn't necessarily read a digest the day it's
# drafted: a Tuesday work-up reviewed on Saturday would otherwise have had its board deleted before
# anyone looked. The current board (`scored-latest.json`) is always live regardless.
RUNS_RETENTION_DAYS = 7


# ===================================================== posting identity
# Every surface that mentions a job — the hourly Telegram alert and the daily digest — needs to
# compare URLs the same way, so "is this apply link just the posting under another spelling?" gets
# one answer everywhere.

_TRACKING_PARAM = re.compile(r"^(utm_[^=]*|ref|referer|referrer|source|src|gclid|fbclid|mc_cid|mc_eid)=", re.I)


def norm_url(url: str) -> str:
    """Normalize a posting URL for identity comparison.

    **Keeps the query string** — it carries the job id on a lot of boards (HN ``item?id=``,
    Greenhouse ``?gh_jid=``, and plenty of employer careers pages put the id there). Dropping it
    collapses many distinct postings per board into one key, which silently mass-flags unrelated
    jobs. Only obvious tracking params are stripped; the remainder is sorted so param order
    doesn't matter.
    """
    u = (url or "").strip().lower().split("#")[0]
    if "?" not in u:
        return u.rstrip("/")
    base, _, query = u.partition("?")
    keep = sorted(p for p in query.split("&") if p and not _TRACKING_PARAM.match(p))
    base = base.rstrip("/")
    return f"{base}?{'&'.join(keep)}" if keep else base


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def prune_runs(days: int = RUNS_RETENTION_DAYS, now: float | None = None) -> list[str]:
    """Drop ``state/runs/<run-date>/`` directories untouched for ``days``. Returns the names removed.

    Ages on the NEWEST file in each dir, so a board that's still being written can't be pruned out from
    under a live run. ``days <= 0`` disables. Best-effort: never fails a hunt cycle over housekeeping.
    """
    if days <= 0 or not RUNS_DIR.is_dir():
        return []
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    removed: list[str] = []
    for run_dir in sorted(RUNS_DIR.iterdir()):
        try:
            if not run_dir.is_dir():
                continue
            mtimes = [f.stat().st_mtime for f in run_dir.rglob("*") if f.is_file()]
            if not mtimes or max(mtimes) >= cutoff:
                continue
            shutil.rmtree(run_dir)
            removed.append(run_dir.name)
        except OSError:  # pragma: no cover - housekeeping must never break a run
            pass
    return removed


def migrate_legacy_state() -> list[str]:
    """Move runtime files from the pre-``state/`` layout into ``state/``.

    Idempotent and safe to call on every run: a file moves only when the legacy path
    exists and the new one does not, so a half-finished migration self-heals and a
    completed one is a no-op. Deliberately does this in code rather than by hand
    because the hourly scheduled task races any manual move — and dropping
    ``seen.json`` would make the next cycle treat every posting as new.
    """
    ensure_dirs()
    moved: list[str] = []
    for old, new in _LEGACY_MOVES:
        try:
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                moved.append(new.name)
        except OSError:  # pragma: no cover - best-effort; never fail a run over this
            pass

    # sweep loose deploy/delegate logs out of out/ into state/logs/
    for pattern in _LEGACY_LOG_GLOBS:
        for old in OUT_DIR.glob(pattern):
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                target = LOGS_DIR / old.name
                if not target.exists():
                    shutil.move(str(old), str(target))
                    moved.append(f"logs/{old.name}")
            except OSError:  # pragma: no cover
                pass

    # Per-run raw boards: out/<run-date>/{jobs,scored}.json -> state/runs/<run-date>/. Sweeping these
    # leaves each run dir holding only what it produced FOR the owner — the resumes, cover letters,
    # research, and digest. Deliberately leaves every other file where it is: only the two known raw
    # names move.
    for name in sorted(_RAW_ARTIFACTS):
        for old in OUT_DIR.glob(f"*/{name}"):
            try:
                new = resolve_raw_artifact(old)
                if new == old or new.exists():
                    continue
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                moved.append(f"runs/{old.parent.name}/{old.name}")
            except OSError:  # pragma: no cover
                pass

    # drop now-empty legacy dirs so the layout reads cleanly
    legacy_hourly = OUT_DIR / "hourly"
    try:
        if legacy_hourly.is_dir() and not any(legacy_hourly.iterdir()):
            legacy_hourly.rmdir()
    except OSError:  # pragma: no cover
        pass
    for run_dir in list(OUT_DIR.glob("*")):
        try:
            if run_dir.is_dir() and not any(run_dir.iterdir()):
                run_dir.rmdir()
        except OSError:  # pragma: no cover
            pass
    return moved
