#!/usr/bin/env python3
"""The unknown-owner open-work picker — draining the register's `whose_move: "unknown"` backlog.

The work-item register (`loops.py`) carries `whose_move: "unknown"` on any row nobody has classified
yet — often a large share of it right after a bulk import. An unknown is NEVER guessed and ALWAYS
renders on both the owner's list and the assistant's; this module is the other half, so the backlog
drains itself instead of sitting as a standing tax nobody opens. The contract:

  1. The daily Brief surfaces the number of unknowns.
  2. The assistant asks once, after the owner's first REAL message following the Brief (acks and
     reactions don't count); the window resets each time the Brief goes out.
  3. If the owner says yes, the unknowns come in batches of 5; batches keep coming until the owner
     stops answering, says stop, or the pool is empty.
  4. Each row offers five owner choices — Owner / Assistant / Both / External / Unknown — rendered as
     a Telegram grid picker when the optional `telegram_ask` module is installed.
  5. Two TERMINAL choices sit beside the five: **Archived** and **Done**. A bulk-imported backlog is
     full of work the owner has long since finished or abandoned elsewhere; an ownership question
     about a dead item has no right answer, so the grid needs a way to say *this one is over*. Neither
     touches `whose_move`.

This module owns the arithmetic and the three durable state files that back it; `telegram_ask.py`
(optional) owns the picker itself; `presence.py` wires the dispatch (the callback path applies an
answered batch and keeps sending batches per clause 3) and the first-real-message trigger (clause 2).

## Without `telegram_ask`: a plain-text batch, answered in chat

`telegram_ask` is an optional import (`ta is None` when it is not installed). `ask()` then degrades
to ONE plain `telegram_send.py` message (via `sentinel.send_telegram`, to the env file's default
chat — `--chat-id` is ignored) listing the batch numbered, with the seven choice labels, and asking
the owner to reply in chat (e.g. *"1 owner, 2 done, 3 both"*). There is no callback in that mode:
the warm session reads the reply and records it with `owi_unknowns.py apply '{"<id>": "<label>"}'`,
then runs `ask` again for the next batch. The result carries `"mode": "text"` so a caller can tell.
Every other verb — `count`, `batch`, `apply`, `brief-line`, the stop/ask-gate state — is pure
arithmetic over `loops.py`'s store and works identically either way.

## The three stores, and why each is separate

* `owi-unknowns-cursor.json` — the STOP/CONTINUE state for the batch loop. Holds `stopped` (clause 3's
  "or tell you to stop") and the last batch sent, for observability. **Not an exclusion list** — which
  items are still `whose_move: unknown` is `loops.py`'s own store, the single source of truth; this
  file only says whether the loop is allowed to keep asking.
* `owi-unknowns-confirmed.json` — items the owner explicitly confirmed `Unknown`. An explicit Unknown
  IS an answer: a row the owner looked at and said *I genuinely don't know* is a resolution, not a
  re-ask candidate, so it drops out of future batches even though `loops.py` still (correctly)
  carries `whose_move: "unknown"` for visibility. A SEPARATE store rather than a new `loops.py`
  field, because "was this already asked about" is bookkeeping specific to the picker loop, not a
  fact about the work item itself.
* `owi-unknowns-ask.json` — the first-real-message gate (clause 2). `brief_at` is stamped every time
  the Brief goes out; `asked_at` is stamped the first time a REAL message after that arrives. The ask
  fires at most once per Brief cycle: `should_ask()` is true only while `asked_at` is null or older
  than `brief_at`.

All three are gitignored `state/` files, written through `stateio.write_json_atomic` — never a bare
`open(p, "w")`.

## The resolution write: `loops.update`'s `whose_move`, not a second writer

`apply()` calls `loops.update(..., whose_move=...)` — the one place `whose_move` can be corrected
after `add()` mints it. There is deliberately no second writer of `open-loops.json` anywhere here.

## The two terminal choices, and why `Archived` rides the OWNER-ONLY verb

* **Done → `loops.resolve`** (`status: "done"`): the row's `terminal_state` event happened, by the
  owner's word. The assistant's own verb, because `done` is a fact about the world either may report.
* **Archived → `loops.owner_abandon`** (`status: "abandoned"`): `abandoned` is one of the writes that
  belong to the owner alone — *it is over, and nobody ever decided that* — with no assistant-side
  code path. **The owner's answer IS the owner acting**: the batch goes to the owner's chat only, the
  answer is the owner choosing Archived, and `apply()` only relays that choice to the verb the owner
  owns. Nothing in this module decides an item is abandoned (an assistant-derived `abandoned` off a
  timestamp is exactly what the owner-only verb exists to prevent). `Archived` rather than `dropped`
  because no reason was given — archiving is *it is over*, not *I decided X* — and `dropped`
  REQUIRES one.

Both drop the item from every future batch (terminal rows never enter `_unresolved`), and both leave
`whose_move` exactly as it was — the ownership question was never answered, it was mooted. Any
mirror in the data store stays the owner's to flip: `apply()` reports each terminal item's
`renders_elsewhere` URL so the batch's report line can list what to update, and writes nothing there.

## The Brief's count and reset are CODE, not a sentence the run remembers

Clause 1's count and the reset clause 2 hangs off (`brief_at`) are not steps the Brief turn is asked
to remember — a written step a model turn decides whether to honour is sometimes not honoured, and a
Brief that forgot the reset would leave the PRIOR day's ask window open. Both belong to
`presence.py`'s slot machinery: the `morning-brief` prompt gets `brief_line()`'s rendered text
appended at launch (the number is computed here and printed verbatim), and `note_brief_sent()` runs
the moment that slot's `claude -p` exits 0 — the daemon's own definition of "the Brief completed".

CLI:
    owi_unknowns.py count                              # int, non-terminal unknowns not yet confirmed
    owi_unknowns.py batch --n 5 [--cursor PATH]         # next N unknowns, oldest first, READ-ONLY
    owi_unknowns.py apply '<{"loop-…": "Owner", …}>'    # writes each choice through loops.update
    owi_unknowns.py ask [--n 5] [--env-file PATH] [--chat-id ID] [--dry-run]
                                                         # composes + sends one batch (grid, or
                                                         # plain text without telegram_ask)
    owi_unknowns.py on-answer --resolution JSON --env-file PATH [--n 5]
                                                         # apply(), then send the NEXT batch unless
                                                         # stopped or the pool is empty (clause 3)
    owi_unknowns.py stop                                # owner said stop — honoured until the next Brief
    owi_unknowns.py should-ask                          # exit 0 = fire the chat-side ask now
    owi_unknowns.py mark-asked                          # stamp asked_at (fires the ask at most once)
    owi_unknowns.py brief-line                          # the Brief's rendered count line (or none)
    owi_unknowns.py note-brief-sent                      # the DAEMON calls this once the Brief slot
                                                         # exits clean — resets the ask window + stop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402
import paths  # noqa: E402
import stateio  # noqa: E402

try:  # the grid picker is optional — see "Without `telegram_ask`" in the module docstring
    import telegram_ask as ta  # noqa: E402
except ImportError:  # pragma: no cover — exercised by the text-fallback tests via mock
    ta = None

CURSOR_FILE = "owi-unknowns-cursor.json"
CONFIRMED_FILE = "owi-unknowns-confirmed.json"
ASK_GATE_FILE = "owi-unknowns-ask.json"

CURSOR_SCHEMA = "seneschal.owi-unknowns-cursor/1"
CONFIRMED_SCHEMA = "seneschal.owi-unknowns-confirmed/1"
ASK_GATE_SCHEMA = "seneschal.owi-unknowns-ask/1"

#: The five owner choices, then the two terminal choices APPENDED — the first five keep their
#: callback indices 0-4, so a grid sent before a choice is added resolves the same way after it.
OWNER_CHOICES = ["Owner", "Assistant", "Both", "External", "Unknown"]
TERMINAL_CHOICES = ["Archived", "Done"]
CHOICES = OWNER_CHOICES + TERMINAL_CHOICES
#: Choice label -> `loops.WHOSE_MOVE` value. `Unknown` maps to itself — a confirmed unknown is still
#: `whose_move: "unknown"` in the register (its visibility never changes); what changes is only
#: whether THIS module asks about it again (see CONFIRMED_FILE above).
CHOICE_TO_WHOSE_MOVE = {"Owner": "owner", "Assistant": "assistant", "Both": "both",
                        "External": "external", "Unknown": "unknown"}
#: Terminal choice label -> the `loops.py` verb that writes it and the status it lands as. See the
#: module docstring for why `Archived` is the owner-only `owner_abandon` and not an assistant verb.
CHOICE_TO_TERMINAL = {"Archived": ("owner_abandon", "abandoned"), "Done": ("resolve", "done")}

#: `--meta`'s `kind`, read by `presence.py`'s callback dispatch to route a Done tap here rather than
#: to another picker's handler — the two are mutually exclusive per record, never both.
ASK_META_KIND = "owi-unknowns"

DEFAULT_BATCH_N = 5


# --------------------------------------------------------------------------- small stores

def _path(filename: str, state_dir: str | None) -> str:
    return os.path.join(paths.state_dir(state_dir), filename)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _read_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return dict(default)
    except (OSError, ValueError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _load_cursor(state_dir: str | None) -> dict:
    return _read_json(_path(CURSOR_FILE, state_dir),
                      {"schema": CURSOR_SCHEMA, "stopped": False, "stopped_at": None,
                       "last_batch_ids": [], "last_batch_at": None})


def _save_cursor(state_dir: str | None, cursor: dict) -> None:
    cursor["schema"] = CURSOR_SCHEMA
    stateio.write_json_atomic(_path(CURSOR_FILE, state_dir), cursor)


def _load_confirmed(state_dir: str | None) -> dict:
    rec = _read_json(_path(CONFIRMED_FILE, state_dir), {"schema": CONFIRMED_SCHEMA, "confirmed": {}})
    confirmed = rec.get("confirmed")
    return confirmed if isinstance(confirmed, dict) else {}


def _save_confirmed(state_dir: str | None, confirmed: dict) -> None:
    stateio.write_json_atomic(_path(CONFIRMED_FILE, state_dir),
                              {"schema": CONFIRMED_SCHEMA, "confirmed": confirmed})


def _load_gate(state_dir: str | None) -> dict:
    return _read_json(_path(ASK_GATE_FILE, state_dir),
                      {"schema": ASK_GATE_SCHEMA, "brief_at": None, "asked_at": None})


def _save_gate(state_dir: str | None, gate: dict) -> None:
    gate["schema"] = ASK_GATE_SCHEMA
    stateio.write_json_atomic(_path(ASK_GATE_FILE, state_dir), gate)


# --------------------------------------------------------------------------- the population

def _unresolved(state_dir: str | None) -> list:
    """Every open-work row that still needs an owner asked about: `whose_move: "unknown"`,
    non-terminal, and not already confirmed-Unknown by a prior answer. Oldest-first, then id — the
    same stable ordering `loops.items()` uses, so two calls with nothing resolved in between return
    the same batch rather than reshuffling."""
    store = loops.load(state_dir)
    confirmed = _load_confirmed(state_dir)
    rows = [r for r in store["items"].values()
            if r.get("whose_move") == "unknown" and r.get("status") not in loops.TERMINAL_STATUSES
            and r.get("id") not in confirmed]
    rows.sort(key=lambda r: (r.get("opened") or "", r.get("id") or ""))
    return rows


def count(state_dir: str | None = None) -> int:
    """How many unknowns are left to ask about — the Brief's one line, never hand-typed."""
    return len(_unresolved(state_dir))


#: The Brief's one line for clause 1 — a rendered sentence rather than a bare integer so the run
#: has nothing to compose (and therefore nothing to get wrong) around the number.
BRIEF_LINE_TEMPLATE = "📋 Unassigned work: {n} item{s} with no owner yet (say \"assign\" to go through them 5 at a time)"


def brief_line(state_dir: str | None = None) -> str | None:
    """The Brief's rendered unassigned-work line, or `None` when the count is 0 (the section is
    omitted, same as every other empty section). The number is `count()`'s — computed by code at the
    moment the daemon launches the Brief, never model-written."""
    n = count(state_dir)
    if n <= 0:
        return None
    return BRIEF_LINE_TEMPLATE.format(n=n, s="" if n == 1 else "s")


def batch(state_dir: str | None = None, n: int = DEFAULT_BATCH_N) -> list:
    """The next `n` unresolved unknowns, oldest first. READ-ONLY — it touches no store. `--cursor`
    (accepted by the CLI for shape compatibility) names where `ask()`'s bookkeeping lives; this
    function itself needs no cursor, because the population is derived fresh from `loops.py`'s own
    store every call."""
    return _unresolved(state_dir)[:max(0, n)]


def apply(state_dir: str | None = None, resolution: dict | None = None,
         now: datetime | None = None) -> dict:
    """Write each `{item_id: choice_label}` pair through `loops.update`'s `whose_move` — or, for the
    two terminal choices, through `loops.resolve` (Done) / the owner-only `loops.owner_abandon`
    (Archived), see `_apply_terminal`. Never a second writer of `open-loops.json`. A row the owner
    left untouched (not present in `resolution`) is left alone — untouched means unchanged, never
    guessed. `mirror` in the result lists every terminal answer with its `renders_elsewhere`, for
    the report line (`mirror_line`)."""
    resolution = resolution or {}
    confirmed = _load_confirmed(state_dir)
    confirmed_changed = False
    results = {}
    mirror = []
    for item_id, choice in resolution.items():
        if choice in CHOICE_TO_TERMINAL:
            res = _apply_terminal(state_dir, item_id, choice, now)
            results[item_id] = res
            if res["ok"]:
                if item_id in confirmed:  # a terminal row never re-batches; keep the store honest
                    confirmed.pop(item_id)
                    confirmed_changed = True
                mirror.append({"id": item_id, "choice": choice, "status": res["status"],
                               "text": res["text"], "renders_elsewhere": res["renders_elsewhere"]})
            continue
        mapped = CHOICE_TO_WHOSE_MOVE.get(choice)
        if mapped is None:
            results[item_id] = {"ok": False, "error": f"unrecognized choice {choice!r}"}
            continue
        try:
            loops.update(state_dir, item_id=item_id, whose_move=mapped, now=now)
        except loops.LoopsError as e:
            results[item_id] = {"ok": False, "error": str(e)}
            continue
        if mapped == "unknown":
            # An EXPLICIT Unknown is an answer — record it so this item is not re-offered next
            # batch, distinct from a row the owner never touched at all.
            if item_id not in confirmed:
                confirmed[item_id] = _stamp(now)
                confirmed_changed = True
        elif item_id in confirmed:
            # A real owner assigned after a previous confirmed Unknown (a re-ask, or a stale
            # confirmation) — it is no longer a confirmed-unknown, so future batches may raise it
            # again if it ever reverts.
            confirmed.pop(item_id)
            confirmed_changed = True
        results[item_id] = {"ok": True, "whose_move": mapped}
    if confirmed_changed:
        _save_confirmed(state_dir, confirmed)
    return {"ok": True, "results": results, "mirror": mirror}


def _apply_terminal(state_dir: str | None, item_id: str, choice: str, now: datetime | None) -> dict:
    """Route one Archived/Done answer to its `loops.py` verb (`CHOICE_TO_TERMINAL`). Never touches
    `whose_move`. The owner's answer IS the owner acting, which is what lets `Archived` travel the
    owner-only `owner_abandon` path (module docstring) — there is no second writer here, only the
    relay."""
    verb, status = CHOICE_TO_TERMINAL[choice]
    try:
        if verb == "owner_abandon":
            # OWNER ONLY. This call is the owner's Archived answer relayed verbatim — the daemon
            # never chooses it, and no timestamp or heuristic reaches this line.
            item = loops.owner_abandon(state_dir, item_id=item_id, now=now)
        else:
            item = loops.resolve(state_dir, item_id=item_id, now=now,
                                 because="the owner marked it Done on the unknown-owner picker")
    except loops.LoopsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "status": status, "text": item.get("text") or item_id,
            "renders_elsewhere": item.get("renders_elsewhere")}


def mirror_line(mirror: list) -> str:
    """The report clause after a batch: which data-store rows the owner should now update
    themselves, one per terminal answer that `renders_elsewhere` somewhere. Empty string when nothing
    needs mirroring — a terminal item with no store row (a PR, a spec header) is finished with the
    register write alone. The update stays the owner's: this names the rows, it never writes them."""
    parts = [f"{m['text']} → {m['choice']}: {m['renders_elsewhere']}"
             for m in mirror if m.get("renders_elsewhere")]
    if not parts:
        return ""
    plural = "s" if len(parts) != 1 else ""
    return f"mirror in your data store yourself ({len(parts)} row{plural}): " + "; ".join(parts)


# --------------------------------------------------------------------------- sending

def _text_batch(question: str, items: list) -> str:
    """The plain-text fallback body (no `telegram_ask`): the batch numbered, the choice vocabulary,
    and how to answer — the reply is read in chat and recorded with `apply`."""
    lines = [question, ""]
    lines += [f"{i}. {it['label']}" for i, it in enumerate(items, 1)]
    lines += ["", "Choices: " + " / ".join(CHOICES),
              "Reply with a number and a choice per line (e.g. \"1 owner, 2 done\"), or \"stop\"."]
    return "\n".join(lines)


def _ask_text(state_dir: str | None, env_file: str | None, question: str, items: list, *,
              dry_run: bool, send_text=None) -> dict:
    """Send one batch as a plain `telegram_send.py` message. `send_text(text, env_file)` is the test
    seam; the default is `sentinel.send_telegram` (imported lazily — only this path needs it)."""
    body = _text_batch(question, items)
    if dry_run:
        return {"ok": True, "dry_run": True, "sent": False, "mode": "text", "body": body,
                "items": len(items)}
    if send_text is None:
        import sentinel  # noqa: E402 — lazy: the grid path and every pure verb never need it
        send_text = sentinel.send_telegram
        env_file = env_file or sentinel.DEFAULT_TELEGRAM_ENV
    res = send_text(body, env_file) or {}
    if not res.get("ok"):
        return {"ok": False, "sent": False, "mode": "text",
                "error": res.get("error") or "telegram_send.py failed"}
    return {"ok": True, "sent": True, "mode": "text", "items": len(items)}


def ask(state_dir: str | None, env_file: str | None, *, n: int = DEFAULT_BATCH_N,
        chat_id: str | None = None, dry_run: bool = False, now: datetime | None = None,
        api=None, send_text=None) -> dict:
    """Compose and send ONE batch of the next `n` unresolved unknowns — a grid picker when
    `telegram_ask` is installed, otherwise the plain-text fallback (module docstring). Honors the stop
    flag (clause 3) and returns `{"ok": True, "sent": False, "reason": …}` rather than sending when
    stopped or when the pool is empty — never an error, both are ordinary stop conditions."""
    cursor = _load_cursor(state_dir)
    if cursor.get("stopped"):
        return {"ok": True, "sent": False, "reason": "stopped — waiting for the next Brief"}
    rows = batch(state_dir, n=n)
    if not rows:
        return {"ok": True, "sent": False, "reason": "no unknowns remaining"}
    items = [{"id": r["id"], "label": r.get("text") or r["id"]} for r in rows]
    plural = "s" if len(items) != 1 else ""
    question = f"{len(items)} open-work item{plural} with no owner yet — who does each belong to?"
    if ta is None:
        res = _ask_text(state_dir, env_file, question, items, dry_run=dry_run, send_text=send_text)
    else:
        c = ta.cfg(ta.load_env(env_file))
        if chat_id:
            c["chat_id"] = chat_id
        res = ta.ask_grid(c, paths.state_dir(state_dir), question, items, CHOICES, dry_run=dry_run,
                          api=api, now=now, meta={"kind": ASK_META_KIND, "batch": len(items)})
    if res.get("ok") and not dry_run:
        cursor["last_batch_ids"] = [it["id"] for it in items]
        cursor["last_batch_at"] = _stamp(now)
        _save_cursor(state_dir, cursor)
    return res


def on_answer(state_dir: str | None, env_file: str | None, resolution: dict, *,
             n: int = DEFAULT_BATCH_N, chat_id: str | None = None, now: datetime | None = None,
             api=None, send_text=None) -> dict:
    """The answered-batch continuation (clause 3: batches stop when the owner stops answering or
    says stop) — `apply()` the batch just answered, then send the NEXT one unless stopped or nothing
    is left. One function so `presence.py`'s callback dispatch is a single subprocess call rather
    than two racing ones."""
    applied = apply(state_dir, resolution, now=now)
    remaining = count(state_dir)
    mirror = mirror_line(applied.get("mirror") or [])
    if remaining == 0:
        return {"ok": True, "applied": applied, "next_sent": False, "remaining": 0,
                "reason": "no unknowns remaining", "mirror": mirror}
    nxt = ask(state_dir, env_file, n=n, chat_id=chat_id, now=now, api=api, send_text=send_text)
    return {"ok": True, "applied": applied, "next_sent": bool(nxt.get("sent")), "remaining": remaining,
            "next": nxt, "mirror": mirror}


def stop(state_dir: str | None = None, now: datetime | None = None) -> dict:
    """The owner said stop. Honoured until the next Brief resets it (`note_brief_sent`)."""
    cursor = _load_cursor(state_dir)
    cursor["stopped"] = True
    cursor["stopped_at"] = _stamp(now)
    _save_cursor(state_dir, cursor)
    return {"ok": True, "stopped": True}


# --------------------------------------------------------------------------- the first-real-message gate

#: A conservative, narrow allowlist of whole-message acknowledgements that must NOT count as the
#: owner's "first real message" (clause 2: acks don't count). Matched against the FULL message,
#: case-folded, trailing `!`/`.` stripped — a message that merely CONTAINS one of these words still
#: counts as real. A false negative here only delays the ask to the next message, which costs
#: nothing; a false positive would fire the ask on a bare ack, which is the one thing excluded.
ACK_ONLY_PHRASES = {
    "took'em", "took em", "took them", "done", "did it", "yep", "yup", "yes", "no", "ok", "okay",
    "kk", "k", "ack", "acked", "got it", "on it", "thanks", "ty", "thank you", "👍", "🙏",
}


def is_real_message(m: dict) -> bool:
    """A REAL message per clause 2: plain text the owner typed, not a reaction/edit/picker tap and
    not a bare reminder-ack phrase. A daemon-synthesized line never reaches here in the first place —
    it is never one of the polled `m` dicts `presence.py` hands this function, only ever appended
    straight to the outbound queue."""
    if not isinstance(m, dict):
        return False
    if m.get("kind") in ("reaction", "edit", "callback"):
        return False
    text = (m.get("text") or "").strip()
    if not text:
        return False
    return text.rstrip("!.").strip().lower() not in ACK_ONLY_PHRASES


def note_brief_sent(state_dir: str | None = None, now: datetime | None = None) -> dict:
    """Called by `presence.py` once the `morning-brief` slot has exited clean (the daemon's
    definition of "the Brief delivered") — resets the first-real-message window (a fresh `brief_at`,
    `asked_at` cleared) AND clears any `stop` from the prior cycle: stop means stop for this cycle's
    batches, not forever."""
    gate = _load_gate(state_dir)
    gate["brief_at"] = _stamp(now)
    gate["asked_at"] = None
    _save_gate(state_dir, gate)
    cursor = _load_cursor(state_dir)
    if cursor.get("stopped"):
        cursor["stopped"] = False
        cursor["stopped_at"] = None
        _save_cursor(state_dir, cursor)
    return {"ok": True, "brief_at": gate["brief_at"]}


def should_ask(state_dir: str | None = None, now: datetime | None = None) -> bool:
    """Fire the chat-side "want to assign any?" ask now? True only when a Brief has actually gone out,
    nothing has asked since, and there is something to ask about. `now` is accepted for test
    determinism but the gate itself is a pure comparison of two stamps, so it is unused beyond that."""
    del now
    gate = _load_gate(state_dir)
    brief_at = gate.get("brief_at")
    if not brief_at:
        return False  # no Brief has ever run this store — nothing to reset the window against
    asked_at = gate.get("asked_at")
    if asked_at and asked_at >= brief_at:
        return False  # already asked this cycle
    return count(state_dir) > 0


def mark_asked(state_dir: str | None = None, now: datetime | None = None) -> dict:
    """Stamp `asked_at` — the ask fires AT MOST ONCE per Brief cycle regardless of the answer:
    anything but a clear yes means no re-ask until the next Brief."""
    gate = _load_gate(state_dir)
    gate["asked_at"] = _stamp(now)
    _save_gate(state_dir, gate)
    return {"ok": True, "asked_at": gate["asked_at"]}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The open-work unknown-owner picker.")
    p.add_argument("--state-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("count", help="how many unresolved unknowns are left")

    b = sub.add_parser("batch", help="the next N unresolved unknowns, oldest first — READ-ONLY")
    b.add_argument("--n", type=int, default=DEFAULT_BATCH_N)
    b.add_argument("--cursor", default=None, help="unused by this READ-ONLY verb; accepted for "
                                                   "shape compatibility")

    ap = sub.add_parser("apply", help="write a {item_id: choice} resolution through loops.update")
    ap.add_argument("resolution", help='JSON object, e.g. \'{"loop-...": "Owner"}\'')

    a = sub.add_parser("ask", help="compose + send the next batch (a Telegram grid picker, or plain "
                                   "text when telegram_ask is not installed)")
    a.add_argument("--n", type=int, default=DEFAULT_BATCH_N)
    a.add_argument("--env-file", default=None)
    a.add_argument("--chat-id", default=None)
    a.add_argument("--dry-run", action="store_true")

    oa = sub.add_parser("on-answer", help="apply an answered batch's resolution, then send the next "
                                          "batch unless stopped or the pool is empty")
    oa.add_argument("--resolution", required=True, metavar="JSON")
    oa.add_argument("--env-file", default=None)
    oa.add_argument("--chat-id", default=None)
    oa.add_argument("--n", type=int, default=DEFAULT_BATCH_N)

    sub.add_parser("stop", help="the owner said stop — honoured until the next Brief")
    sub.add_parser("should-ask", help="exit 0 iff the first-real-message ask should fire now")
    sub.add_parser("mark-asked", help="stamp asked_at (fires the ask at most once per Brief cycle)")
    sub.add_parser("brief-line", help="the Brief's rendered unassigned-work line; empty when 0")
    sub.add_parser("note-brief-sent", help="the daemon calls this once the Brief slot exits clean — "
                                           "resets the ask window and any stop")

    args = p.parse_args(argv)
    sd = args.state_dir

    if args.cmd == "count":
        print(json.dumps({"ok": True, "count": count(sd)}))
        return 0
    if args.cmd == "batch":
        print(json.dumps({"ok": True, "items": batch(sd, n=args.n)}, ensure_ascii=False))
        return 0
    if args.cmd == "apply":
        try:
            resolution = json.loads(args.resolution)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"resolution is not JSON: {e}"}))
            return 2
        if not isinstance(resolution, dict):
            print(json.dumps({"ok": False, "error": "resolution must be a JSON object"}))
            return 2
        print(json.dumps(apply(sd, resolution), ensure_ascii=False))
        return 0
    if args.cmd == "ask":
        res = ask(sd, args.env_file, n=args.n, chat_id=args.chat_id, dry_run=args.dry_run)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if args.cmd == "on-answer":
        try:
            resolution = json.loads(args.resolution)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"--resolution is not JSON: {e}"}))
            return 2
        if not isinstance(resolution, dict):
            print(json.dumps({"ok": False, "error": "--resolution must be a JSON object"}))
            return 2
        res = on_answer(sd, args.env_file, resolution, n=args.n, chat_id=args.chat_id)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if args.cmd == "stop":
        print(json.dumps(stop(sd), ensure_ascii=False))
        return 0
    if args.cmd == "should-ask":
        fire = should_ask(sd)
        print(json.dumps({"ok": True, "should_ask": fire}))
        return 0 if fire else 1
    if args.cmd == "mark-asked":
        print(json.dumps(mark_asked(sd), ensure_ascii=False))
        return 0
    if args.cmd == "brief-line":
        line = brief_line(sd)
        print(json.dumps({"ok": True, "count": count(sd), "line": line}, ensure_ascii=False))
        return 0
    # note-brief-sent
    print(json.dumps(note_brief_sent(sd), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
