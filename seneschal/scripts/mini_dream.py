#!/usr/bin/env python3
"""Mini-dream — per-session distillation into the assistant's shared cross-instance memory (phase 2b).

Phase 2b of live-session awareness (the session registry is phase 2). When any Claude Code session on
the machine **ends**, the machine-wide ``session_stamp.py`` hook spawns this script detached; it distills
the session's transcript into one compact record appended to ``state/session-distillations.jsonl`` — the
fast append path of the LSM shape. Every assistant surface reads the tail of that log at orientation
("what did the other instances do lately?"), and the nightly **Dream** run is the slow compaction path:
it ingests new distillates into the local RAG index (so semantic recall finds them later) and prunes
entries older than ``--prune-days`` (default 30).

The **salience ladder** keeps it cheap and honest:
  * fewer than ``MIN_USER_TURNS`` real user turns → **skip** (nothing worth remembering);
  * a small session → **deterministic** distillate (free: title, first ask, files touched, branch);
  * ``LLM_USER_TURNS``+ turns → a **headless LLM distill** (``claude -p``, cheap-model default, the
    same subscription-billing rule as the daemon: ``ANTHROPIC_API_KEY`` is scrubbed), falling back to
    the deterministic record on any failure — a distillate always lands, an LLM just makes it better.

Anchoring: the output lives in **the assistant's home state dir** (script-relative ``../state``) no
matter what project the session ran in — that's the whole point (a per-project memory tool dreams into
*per-project* memory; the mini-dream is the assistant-anchored counterpart, see ``references/memory.md``).
Recursion guard: the LLM child runs with ``SENESCHAL_MINI_DREAM=1`` so its own SessionEnd never spawns
another distiller. Idempotent per session id. Stdlib only.

Usage:
  python mini_dream.py --transcript FILE --session-id ID [--cwd DIR] [--engine auto|deterministic|llm]
  python mini_dream.py --prune-days 30          # compaction (Dream calls this nightly)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:  # owner-timezone rendering for record stamps (guarded — presence.py house style)
    import tz_common as _tz_common
except ImportError:  # pragma: no cover — a bare interpreter still stamps machine-local
    _tz_common = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# Windows: run the `claude -p` distill with a HIDDEN console. session_stamp spawns THIS distiller detached
# (DETACHED_PROCESS → console-less), so a console child like claude would otherwise get a fresh VISIBLE
# window that steals focus (the "random Claude windows" on SessionEnd). CREATE_NO_WINDOW hides it; the
# captured stdio is unaffected — the same two-level fix the daemon tree uses (see sentinel.NO_WINDOW).
# 0 (no-op) off Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DISTILL_FILE = "session-distillations.jsonl"
RECURSION_ENV = "SENESCHAL_MINI_DREAM"  # set on the LLM child so its SessionEnd never re-spawns a distiller
MIN_USER_TURNS = 2    # below this the session isn't worth a record at all
LLM_USER_TURNS = 6    # at/above this, try the LLM distill (deterministic below, and as the fallback)
DEFAULT_MODEL = "haiku"  # cheap-tier ALIAS, deliberately not a dated id; the claude CLI resolves it
LLM_TIMEOUT_SEC = 180
MAX_EXCERPT_CHARS = 16000   # transcript excerpt cap fed to the LLM
MAX_FILES_TOUCHED = 12
DEFAULT_PRUNE_DAYS = 30


def _local_stamp(now: datetime | None = None) -> str:
    """Owner-local ISO-8601 stamp for a distillation record — the owner's configured timezone via
    ``tz_common`` when resolvable, the machine clock otherwise. Deliberately ISO (not the human
    ``tz_common.local_stamp`` format): ``prune`` parses it back with ``datetime.fromisoformat``."""
    dt = now if now is not None else datetime.now(timezone.utc)
    if _tz_common is not None:
        try:
            return _tz_common.to_local(dt).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001 — a tz hiccup must never cost the record
            pass
    return dt.astimezone().isoformat(timespec="seconds")


def _block_text(content) -> str:
    """Flatten a message content (string or block list) into plain text."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
    return "\n".join(parts)


def parse_transcript(path: str) -> dict:
    """Tolerantly extract the distillable facts from a Claude Code session transcript (JSONL).
    Sidechain (subagent) traffic is excluded; unparseable lines are skipped — a partial read still
    produces a usable record."""
    info = {"title": None, "first_prompt": None, "last_assistant": None, "user_turns": 0,
            "assistant_turns": 0, "files_touched": [], "branch": None, "cwd": None, "excerpt": []}
    seen_files = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return info
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            kind = d.get("type")
            if kind == "custom-title" and d.get("customTitle"):
                info["title"] = d["customTitle"]
            elif kind == "user":
                text = _block_text((d.get("message") or {}).get("content")).strip()
                # Skip harness scaffolding (system reminders, tool results echoed as user turns).
                if not text or text.startswith("<"):
                    continue
                info["user_turns"] += 1
                info["branch"] = d.get("gitBranch") or info["branch"]
                info["cwd"] = d.get("cwd") or info["cwd"]
                if info["first_prompt"] is None:
                    info["first_prompt"] = text[:400]
                info["excerpt"].append(("Owner", text[:1200]))
            elif kind == "assistant":
                blocks = (d.get("message") or {}).get("content") or []
                text = _block_text(blocks).strip()
                if text:
                    info["assistant_turns"] += 1
                    info["last_assistant"] = text[:800]
                    info["excerpt"].append(("Session", text[:1200]))
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        fp = inp.get("file_path") or inp.get("path")
                        if isinstance(fp, str) and fp not in seen_files:
                            seen_files.add(fp)
                            info["files_touched"].append(fp)
    info["files_touched"] = info["files_touched"][:MAX_FILES_TOUCHED]
    return info


def deterministic_distillate(info: dict) -> str:
    """The free distillate — terse but real. Always available; also the LLM-failure fallback."""
    bits = []
    if info.get("title"):
        bits.append(f"Session: {info['title']}.")
    if info.get("first_prompt"):
        bits.append(f"Opened with: {info['first_prompt'][:200]}")
    if info.get("files_touched"):
        bits.append("Touched: " + ", ".join(info["files_touched"][:6])
                    + ("…" if len(info["files_touched"]) > 6 else ""))
    if info.get("last_assistant"):
        bits.append(f"Ended on: {info['last_assistant'][:240]}")
    return "\n".join(bits) or "(empty session)"


def llm_distillate(info: dict, model: str, claude_bin: str, timeout: int = LLM_TIMEOUT_SEC) -> str | None:
    """Headless ``claude -p`` distill of the transcript excerpt. Subscription-billed (the API key is
    scrubbed — same rule as the daemon); the child carries RECURSION_ENV so its own SessionEnd hook
    never spawns another distiller. Returns None on ANY failure — the caller falls back."""
    convo, used = [], 0
    for who, text in reversed(info.get("excerpt") or []):  # newest first, then restore order
        piece = f"{who}: {text}"
        if used + len(piece) > MAX_EXCERPT_CHARS:
            break
        convo.append(piece)
        used += len(piece)
    convo.reverse()
    if not convo:
        return None
    prompt = (
        "Distill this Claude Code session for the assistant's cross-session memory. Output ONLY 3-6 "
        "terse bullets (no preamble): what was worked on, decisions made, durable state changed (files "
        "/ PRs / systems / the store), and any open loops another session should know about.\n\n"
        + (f"Session title: {info['title']}\n" if info.get("title") else "")
        + "Transcript excerpt:\n" + "\n".join(convo)
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env[RECURSION_ENV] = "1"
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, cwd=DEFAULT_STATE_DIR, shell=False,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def _load_lines(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def already_distilled(state_dir: str, session_id: str) -> bool:
    return any(d.get("session_id") == session_id
               for d in _load_lines(os.path.join(state_dir, DISTILL_FILE)))


def distill(state_dir: str, transcript: str, session_id: str, cwd: str | None = None,
            engine: str = "auto", model: str = DEFAULT_MODEL, claude_bin: str = "claude",
            now: datetime | None = None) -> dict:
    """Distill one ended session into the shared log. Idempotent per session id; salience-gated.
    Returns ``{"action": "written"|"skipped_small"|"skipped_dupe", "engine": ...}``."""
    if already_distilled(state_dir, session_id):
        return {"action": "skipped_dupe", "engine": None}
    info = parse_transcript(transcript)
    if info["user_turns"] < MIN_USER_TURNS:
        return {"action": "skipped_small", "engine": None, "user_turns": info["user_turns"]}
    used_engine = "deterministic"
    text = None
    if engine == "llm" or (engine == "auto" and info["user_turns"] >= LLM_USER_TURNS):
        text = llm_distillate(info, model, claude_bin)
        if text:
            used_engine = "llm"
    if text is None:
        text = deterministic_distillate(info)
    record = {
        "id": f"md-{session_id[:12]}",
        "session_id": session_id,
        "ended_at": _local_stamp(now),
        "cwd": cwd or info.get("cwd"),
        "branch": info.get("branch"),
        "title": info.get("title"),
        "engine": used_engine,
        "user_turns": info["user_turns"],
        "files_touched": info["files_touched"],
        "distillate": text,
    }
    path = os.path.join(state_dir, DISTILL_FILE)
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"action": "written", "engine": used_engine, "id": record["id"]}


def prune(state_dir: str, days: int = DEFAULT_PRUNE_DAYS, now: datetime | None = None) -> int:
    """Dream's compaction half: drop distillates older than ``days`` (they've long since been ingested
    into the RAG index, which is where old context is *supposed* to be recalled from). Rewrites the
    file atomically; unparseable lines are dropped as hygiene. Returns the number removed."""
    if now is None:
        now = datetime.now(timezone.utc)
    path = os.path.join(state_dir, DISTILL_FILE)
    rows = _load_lines(path)
    if not rows:
        return 0
    cutoff = now - timedelta(days=days)
    kept = []
    for d in rows:
        try:
            stamp = datetime.fromisoformat(d.get("ended_at", ""))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp >= cutoff:
                kept.append(d)
        except ValueError:
            continue
    removed = len(rows) - len(kept)
    if removed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for d in kept:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return removed


def main() -> int:
    p = argparse.ArgumentParser(description="Mini-dream: distill an ended session into shared memory.")
    p.add_argument("--transcript", help="path to the session's JSONL transcript")
    p.add_argument("--session-id", help="the ended session's id")
    p.add_argument("--cwd", default=None, help="the session's working directory (from the hook event)")
    p.add_argument("--engine", default="auto", choices=["auto", "deterministic", "llm"],
                   help="auto = salience ladder (deterministic below %d user turns)" % LLM_USER_TURNS)
    p.add_argument("--model", default=DEFAULT_MODEL, help="model for the LLM distill")
    p.add_argument("--claude-bin", default="claude", help="claude CLI binary")
    p.add_argument("--prune-days", type=int, default=None,
                   help="prune distillates older than N days and exit (Dream's nightly compaction)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding the distillations log")
    args = p.parse_args()

    if args.prune_days is not None:
        print(json.dumps({"ok": True, "pruned": prune(args.state_dir, args.prune_days)}))
        return 0
    if os.environ.get(RECURSION_ENV):
        print(json.dumps({"ok": True, "action": "skipped_recursion"}))
        return 0
    if not args.transcript or not args.session_id:
        p.error("--transcript and --session-id are required (or use --prune-days)")
    result = distill(args.state_dir, args.transcript, args.session_id, cwd=args.cwd,
                     engine=args.engine, model=args.model, claude_bin=args.claude_bin)
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
