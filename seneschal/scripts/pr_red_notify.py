#!/usr/bin/env python3
"""**Tell the owner when an open PR's CI goes RED — the resident half `pr_sweep.py` does not have.**
Stdlib + the sibling `telegram_send.py` subprocess.

## The gap

`pr_sweep.py`'s resident sweep is **green-only by construction**: :func:`pr_sweep.is_green` requires
a terminal, wholly-passing rollup, and a PR that fails that test is simply *not a candidate* — it
falls out of the pass with no picker and, without this module, **no notification of any kind either.**
`watch_pr.py` DOES compute a red verdict and DOES have a red summary line
(:func:`watch_pr.summary_line`), which reaches the owner through `jobs.py`'s completion push — but
`watch_pr.py` only runs when somebody starts one **as a job**. A PR nobody started a watcher on goes
red silently, and neither fixing it nor telling anyone happens. That is the same shape as the gap
`pr_sweep.py` itself closes ("auto-send when a watcher happens to be running" is not "auto-send"), one
class over: red instead of green.

## What this is, and what it explicitly is not

**A NOTIFICATION, NEVER A PICKER.** A red PR needs fixing, not approving, so this module has no
option list, no `meta.kind`, no tap, and never once names `merge_guard.record_approval` or
`telegram_ask.ask`. It sends one plain message via the sibling `telegram_send.py` — the same
chokepoint `sentinel.send_telegram` already wraps — and that is the whole affordance. Nothing here
can widen what merges: it writes no approval, and it costs `pr_sweep.py`'s resident pass no second
`gh` call, because it reads the SAME rows that pass already fetched for the green check.

## Once per `(repo, pr, head_sha)`, the guard's own key shape

A red PR must not buzz every ~3 minutes forever, so this keeps its own append-only ledger —
`pr-red-notify-log.jsonl`, `state/`'s house shape (`merge-ask-log.jsonl`'s sibling, one file over) —
keyed exactly as `merge_guard`'s ask log is: `(repo, pr, head_sha)`. A new head (a fixup push, a
rebase, a force-push) is a **different** question and notifies again, which is correct: the failure
that made the old head red may not be the failure the new commit carries.

**The log is separate from the ask log, deliberately.** An "asked" row means *the owner was offered a
decision*; a "notified" row means *the owner was told about a failure*. Folding the two into one file
would make a red notification look like a merge ask to anything that reads the ledger for that
purpose, and `merge_guard.already_asked`/`record_ask` exist to answer a narrower question than this one.

## Fail-open and quiet on every path, the same posture as its siblings

`gh` is never called here — the rows are handed in. A Telegram outage, a missing credential, an
unreadable log: each costs **this one notification** and nothing else. **A send that does not
positively confirm `ok: true` records nothing** — the same asymmetry `merge_guard.record_ask` uses for
an ask: marking-then-failing is how a notification gets lost forever, where a duplicate on the next
pass costs one extra buzz. `pr_sweep.sweep` wraps every call into this module in the same
never-raises contract as its other siblings (`merge_guard`, `picker_mark`, `picker_retire`,
`watch_pr`) — the daemon's supervised PR-watch task may not go down because a notification could not
be sent.

## Quiet hours and the burst cap live in `pr_sweep.py`, not here

This module owns the ledger, the message text and the send — it does not decide *when* to ask any of
those questions. `pr_sweep.sweep` reuses its own already-pinned quiet-hours instant
(`merge_guard.in_quiet_hours`, stood down while the owner is demonstrably awake —
`merge_guard.awake_evidence`) and its own per-pass cap (`MAX_RED_NOTIFIES_PER_PASS`) rather than a
second copy of either rule: a resident sweep sees every open PR at once after a restart, and a red PR
going that way at 3 AM is exactly as loud as a green one would be. See `pr_sweep.py`'s module
docstring for the argument; it is not repeated here.

## It never sends the picker's own words

`notify_text` builds its "CI RED" clause from :func:`watch_pr.summary_line` — imported, not
re-derived — because that is the ONE place the rollup-to-prose rule lives, and a second phrasing here
is exactly the kind of drift `pr_sweep.is_green` already refuses by importing `watch_pr.classify`
rather than re-folding the rollup itself.

USAGE (diagnostics; `pr_sweep.py`'s resident sweep is the production caller):
  python pr_red_notify.py --dry-run --pr 12 --repo owner/name --title "…" \
      --head deadbeef --failing "unittest (FAILURE)"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

import merge_guard as mg  # noqa: E402 — DEFAULT_STATE_DIR, DEFAULT_TELEGRAM_ENV, repo_key
import watch_pr  # noqa: E402 — summary_line: the ONE rollup-verdict-to-prose rule, imported not copied

#: `state/`'s house shape — append a line, never rewrite one. `merge_guard.ASK_LOG_FILE`'s sibling,
#: kept in its own file rather than folded in (see the module docstring for why).
NOTIFY_LOG_FILE = "pr-red-notify-log.jsonl"
NOTIFY_LOG_SCHEMA = "seneschal.pr-red-notify/1"

#: The label a notification carries, so the message is unmistakably informational rather than a
#: question — the opposite polarity of a merge picker's "Tap one." line.
RED_EMOJI = "\U0001F534"  # 🔴


def _stamp(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def notify_log_path(state_dir: str) -> str:
    return os.path.join(state_dir, NOTIFY_LOG_FILE)


def read_notifies(state_dir: str) -> list:
    """Every red notification that has gone out, oldest first. Never raises: an unreadable log means
    *nothing has been sent*, which costs at worst one duplicate notice — the safe direction, since the
    alternative is a red PR that silently never gets a second look.

    A torn or unparseable line is skipped rather than aborting the read (`merge_guard.read_asks`'s
    rule): one bad row may not hide every good one."""
    try:
        with open(notify_log_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def notified_for(state_dir: str, pr: int, head_sha: str, repo=None) -> list:
    """Every notify row for **this PR in this repo at this commit**, oldest first."""
    out = []
    for row in read_notifies(state_dir):
        if row.get("pr") != int(pr) or row.get("head_sha") != head_sha:
            continue
        row_repo = row.get("repo")
        if row_repo and repo and mg.repo_key(row_repo) != mg.repo_key(repo):
            continue
        out.append(row)
    return out


def already_notified(state_dir: str, pr: int, head_sha: str, repo=None) -> bool:
    """Has the owner already been told CI is red for **this PR at this commit**? A moved head (a
    fixup, a rebase, a force-push) flips this back to `False`, the same binding
    `merge_guard.already_asked` uses and for the same reason: a different commit is a different
    question."""
    return bool(notified_for(state_dir, pr, head_sha, repo))


def record_notified(state_dir: str, pr: int, head_sha: str, repo=None, now=None) -> None:
    """Note that the owner was told. **APPENDS. Every notification gets a row, and no row is ever
    overwritten** — `merge_guard.record_ask`'s shape, for the same reason: a ledger that can be
    silently reduced to one row cannot be used to find out that two things happened.

    Fail-open, this directory's house rule for `state/` writers: a log we cannot write costs at worst
    a duplicate notice next pass, never the notice itself. Only ever called after a send that
    positively confirmed delivery."""
    row = {"schema": NOTIFY_LOG_SCHEMA, "pr": int(pr), "repo": str(repo) if repo else None,
           "head_sha": str(head_sha), "notified_at": _stamp(now)}
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(notify_log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def notify_text(repo: str, row: dict, verdict: dict) -> str:
    """The message body — repo, title, `watch_pr`'s own red summary, and a bare link, in that order.

    **The link goes out bare**, `merge_guard.pr_link_line`'s rule, one module over: this is a plain
    `TELEGRAM_FORMAT=plain`-safe send, so no markup is attempted around it that a chunker or an
    escaper could mangle."""
    pr = row.get("number")
    title = str(row.get("title") or "").strip()
    head = str(row.get("headRefOid") or "").strip()
    lines = [f"{RED_EMOJI} {repo} #{pr}" + (f" — {title}" if title else "")]
    lines.append(watch_pr.summary_line(str(pr), verdict))
    if head:
        lines.append(f"Head: {head[:12]}")
    url = row.get("url")
    if isinstance(url, str) and url.strip():
        lines.append(url.strip())
    return "\n".join(lines)


def send(text: str, env_file=None, state_dir=None) -> dict:
    """Push one plain notification via the sibling `telegram_send.py`, through `sentinel.send_telegram`
    — the same chokepoint every other prose send in this tree funnels through, to the pull-requests
    topic (`telegram_topics.TOPIC_PULL_REQUESTS`). Always returns a dict, never raises: a Telegram
    outage, a missing credential, an unreadable topic store — each costs this one notification, and
    the caller decides what to do about that (retry next pass).

    Both imports are lazy: this function runs from inside `pr_sweep.py`'s resident daemon task, and an
    import that fails here must cost the notification, not the pass that found it."""
    try:
        import sentinel
        import telegram_topics as tt
    except Exception as e:  # noqa: BLE001 — an import that fails must cost the notice, not the pass
        return {"ok": False, "error": f"could not reach the sender: {e!r}"}
    env_path = env_file or mg.DEFAULT_TELEGRAM_ENV
    return sentinel.send_telegram(text, env_path, topic=tt.TOPIC_PULL_REQUESTS, state_dir=state_dir)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Send (or preview) one red-CI notification for a PR — never a picker, never a merge")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--url", default=None)
    p.add_argument("--failing", action="append", default=[], metavar="NAME (STATE)",
                   help="repeatable; one failing check name, as `watch_pr.classify` would report it")
    p.add_argument("--state-dir", default=mg.DEFAULT_STATE_DIR)
    p.add_argument("--env-file", default=None)
    p.add_argument("--dry-run", action="store_true", help="print the message; send nothing, record nothing")
    args = p.parse_args(argv)

    row = {"number": args.pr, "title": args.title, "headRefOid": args.head, "url": args.url}
    verdict = {"done": True, "passed": 0, "failed": max(1, len(args.failing)),
               "pending": 0, "empty": False, "failing_names": args.failing or ["(unnamed check)"]}
    text = notify_text(args.repo, row, verdict)
    if args.dry_run:
        print(text)
        return 0
    already = already_notified(args.state_dir, args.pr, args.head, repo=args.repo)
    if already:
        print(json.dumps({"ok": True, "sent": False,
                          "reason": "already notified at this head"}, ensure_ascii=False))
        return 0
    res = send(text, env_file=args.env_file, state_dir=args.state_dir)
    if res.get("ok"):
        record_notified(args.state_dir, args.pr, args.head, repo=args.repo)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
