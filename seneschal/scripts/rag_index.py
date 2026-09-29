#!/usr/bin/env python3
"""Build / update the assistant's local semantic RAG index (Advisor Chain, phase B).

Embeds text records with a local Ollama embedder and upserts them into the
sqlite index (``state/rag-index.sqlite``). Incremental: a doc whose text hash
is unchanged since last run is skipped; a changed doc has its old chunks
replaced. See ``RAG_SETUP.md`` and ``rag_common.py``.

Corpus sources
--------------
* ``--local``    index the local prose the assistant owns on disk: ``run-log.md``,
                 ``carry-over.md`` (source = "run-log" etc.). The retired context
                 digest is deliberately NOT here — its replacement stores hold
                 structured records, not prose worth semantically indexing.
* ``--chat``     index ``state/turns.jsonl`` (the owner<->assistant thread) as
                 source ``chat`` — one document per turn, both speakers, ``!private``
                 turns skipped. Only the FILE is read; the ``turns`` module that
                 writes it is not imported.
* ``--ingest F`` index records from a JSONL file, one object per line:
                 ``{"source": "journal", "ref": "2026-07-05", "text": "..."}``.
                 This is how Notion-resident corpus (journal, notes) gets in:
                 a Claude run that HAS the Notion MCP (Dream's nightly refresh)
                 fetches the new entries and writes the JSONL, then calls this.

Salience tags (optional, observe-only — the what's-safe-to-forget experiment)
------------------------------------------------------------------------------
Ingest records may carry two extra keys Dream writes per the rubric in
``../references/salience.md``::

    {"source": "journal", "ref": "2026-07-09", "text": "...",
     "disposable": 1, "salience_cat": "logistics.transient"}

``salience_cat`` is coerced to the closed taxonomy (unknown → "unknown");
``disposable: 1`` is a *logged prediction* ("I won't need this again") accepted
only on eligible categories — on any other it is cleared with a warning
(identity.core can never be predicted away). Tagged rows stay fully retrievable;
nothing here hides or deletes. Absent keys default to 0 / null, so existing
JSONLs are unaffected. See ``SALIENCE_SETUP.md``.

Bitemporal validity (optional — "what is true now", not "what is similar")
---------------------------------------------------------------------------
Ingest records may also carry ``valid_from`` (ISO timestamp — when the fact became
true; defaults to ingest time) and/or ``supersedes`` (the ``ref`` of an older doc in
the SAME source that this record replaces)::

    {"source": "notes", "ref": "supplier-2026-07", "text": "New supplier is Acme.",
     "valid_from": "2026-07-29T00:00:00+00:00", "supersedes": "supplier-2026-03"}

A declared ``supersedes`` stamps the older doc's ``valid_to`` with this record's
``valid_from`` — **only while it is still open** (a second superseder never
overwrites an existing close; the first close is history, not state). The
superseded doc is never deleted: it leaves default answers in ``rag_query.py`` but
stays fully retrievable via ``--include-superseded`` / ``--as-of`` — the salience
posture applied to currency. Records without these keys behave exactly as before.

Provenance guard — mail text is DATA, never INSTRUCTION (`provenance_guard.py`)
-------------------------------------------------------------------------------
:func:`index_records` is the single write choke point, and every record passes
:meth:`provenance_guard.ProvenanceGuard.check` **before its text is read**; a record whose
writer cannot vouch for it is **not persisted**. The check is structural — source name and
producer stamp only, never content. It FAILS CLOSED: an unregistered ``source``, a
stamp-required source arriving without a stamp, and an unrecognised stamp are all refusals.

``session-distillation`` records must therefore carry ``{"provenance": "deterministic-fields"}`` or
``{"provenance": "llm-excerpt"}`` as `mini_dream.py` emits them; the second is refused. Refusals go
to stderr, to the durable ``provenance_refusals`` ledger inside this index (created on open by
:func:`provenance_guard.ensure_ledger`), and to ``--stats``. The one override is
``--allow-unattested SOURCE`` on the command line, and it is counted too.

Examples
--------
    python rag_index.py --local
    python rag_index.py --local --chat
    python rag_index.py --ingest journal.jsonl notes.jsonl
    python rag_index.py --rebuild --local
    python rag_index.py --stats
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import provenance_guard as pg
import rag_common as rc

try:  # speaker labels for the chat corpus; the index must build without a persona configured
    import identity_common as _ic
except ImportError:  # pragma: no cover - identity_common ships beside this file
    _ic = None

LOCAL_SOURCES = {
    "run-log": rc.STATE / "run-log.md",
    "carry-over": rc.STATE / "carry-over.md",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_local():
    for source, path in LOCAL_SOURCES.items():
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                yield {"source": source, "ref": path.name, "text": text}


# --------------------------------------------------------------------------- chat turns
#
# The complete text of something the owner says in chat otherwise survives only as long as the
# warm session's rolling thread (and a short prefix in the cockpit ring). `state/turns.jsonl` keeps
# it verbatim; indexing it here is what makes it findable — a name mentioned once, weeks ago, is
# exactly the recall the thread cannot give back.

CHAT_TURNS_FILE = "turns.jsonl"

# One document per turn, not per row: the two halves of a turn are one exchange, and splitting them
# would index the assistant's answer with no idea what it answered.
CHAT_REF_PREFIX = "turn"

# The `speaker` value `turns.py` writes for the owner's side; anything else is the assistant.
OWNER_SPEAKER = "owner"


def _chat_labels(owner_label=None, assistant_label=None):
    """The speaker labels embedded into chat documents.

    The configured names from `persona/identity.json` when set — so a query that names the owner
    or the assistant finds their turns — else plain "Owner" / "Assistant". Explicit arguments win,
    which is also what keeps the tests independent of whatever persona the machine has."""
    identity = None
    if _ic is not None and (owner_label is None or assistant_label is None):
        try:
            identity = _ic.load_identity()
        except Exception:  # noqa: BLE001 — a label is cosmetic; it must never stop an ingest
            identity = None
    if owner_label is None:
        owner_label = (_ic.get_str(identity, "owner", "name") if identity else None) or "Owner"
    if assistant_label is None:
        assistant_label = (
            (_ic.get_str(identity, "assistant", "name") if identity else None) or "Assistant")
    return owner_label, assistant_label


def _iter_chat_turns(state_dir=None, *, owner_label=None, assistant_label=None):
    """Yield one indexable document per chat turn, both speakers, in order.

    **A `!private` turn is skipped entirely** — the writer records a tombstone with no text, and
    this reader must not resurrect the turn from its metadata either. A redaction that holds in the
    writer and not in the indexer is not a redaction; one redacted row redacts the whole turn,
    whatever order the rows arrive in.

    **`origin: "system"` rows are INCLUDED, with an honest speaker label** — job-completion
    pushes, reaction markers, attachment lines. Dropping them would leave gaps in the conversation
    that make the surrounding turns harder to read, not easier; labelling them is the honest move.

    Deliberately tolerant, like every other reader of `state/`: a corrupt line costs that line, a
    missing file reads empty. Reads the FILE only — the `turns` module that writes it is not
    imported, so this works whether or not that module is installed."""
    state = pathlib.Path(state_dir) if state_dir else rc.STATE
    path = state / CHAT_TURNS_FILE
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    owner_label, assistant_label = _chat_labels(owner_label, assistant_label)
    by_turn = {}
    order = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        tid = rec.get("turn_id")
        if not tid or not isinstance(tid, str):
            continue
        if tid not in by_turn:
            by_turn[tid] = {"redacted": False, "parts": [], "at": rec.get("at")}
            order.append(tid)
        slot = by_turn[tid]
        if rec.get("redacted"):
            slot["redacted"] = True
            slot["parts"] = []
            continue
        if slot["redacted"]:
            continue
        text = rec.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        who = owner_label if rec.get("speaker") == OWNER_SPEAKER else assistant_label
        if rec.get("origin") == "system":
            who += " (system)"
        slot["parts"].append(f"{who}: {text.strip()}")

    for tid in order:
        slot = by_turn[tid]
        if slot["redacted"] or not slot["parts"]:
            continue
        yield {
            "source": "chat",
            "ref": f"{CHAT_REF_PREFIX}:{tid}",
            "text": "\n".join(slot["parts"]),
            # The turn's own timestamp, so `--as-of` and the bitemporal layer place it when it was
            # SAID rather than when Dream happened to ingest it.
            "valid_from": slot.get("at"),
        }


def _iter_jsonl(paths):
    for p in paths:
        for lineno, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  ! {p}:{lineno} skipped (bad JSON: {exc})", file=sys.stderr)
                continue
            if not rec.get("text") or not rec.get("ref"):
                print(f"  ! {p}:{lineno} skipped (missing text/ref)", file=sys.stderr)
                continue
            yield {
                "source": rec.get("source", Path(p).stem),
                "ref": str(rec["ref"]),
                "text": str(rec["text"]),
                "disposable": rec.get("disposable", 0),
                "salience_cat": rec.get("salience_cat"),
                "valid_from": rec.get("valid_from"),
                "supersedes": rec.get("supersedes"),
                # Forwarded so the guard sees the writer's real stamp. Rebuilding the record from a
                # fixed field list that omitted this would turn every correctly-stamped
                # session-distillation into a `missing-provenance-stamp` refusal.
                pg.STAMP_KEY: rec.get(pg.STAMP_KEY),
            }


def _norm_salience(rec):
    """Normalize a record's optional salience tags against the closed vocabulary.

    Unknown category → "unknown" (never passed through raw); a ``disposable=1`` prediction on
    an ineligible category is cleared with a warning — ``identity.core`` (birthdays, the
    family-bereavement class) can never be predicted-disposable, even by a buggy tagging pass.
    See ``../references/salience.md``.
    """
    cat = rec.get("salience_cat")
    if cat is not None:
        cat = str(cat)
        if cat not in rc.SALIENCE_CATEGORIES:
            cat = "unknown"
    disposable = 1 if rec.get("disposable") in (1, True, "1") else 0
    if disposable and cat not in rc.DISPOSABLE_ELIGIBLE:
        print(
            f"  ! {rec.get('source')}:{rec.get('ref')} disposable=1 on ineligible category "
            f"{cat!r} — cleared (see references/salience.md)",
            file=sys.stderr,
        )
        disposable = 0
    return disposable, cat


def _norm_valid_from(rec):
    """Return the record's declared ``valid_from`` when it parses, else None.

    Fail-open like :func:`_norm_salience`: an unparsable timestamp degrades (with a
    warning) to the default — ingest time for a new doc, the stored start for an
    existing one. A bad stamp may cost precision, never the record.
    """
    raw = rec.get("valid_from")
    if raw is None or raw == "":
        return None
    raw = str(raw)
    if rc.parse_ts(raw) is None:
        print(
            f"  ! {rec.get('source')}:{rec.get('ref')} unparsable valid_from {raw!r} "
            f"— using ingest time",
            file=sys.stderr,
        )
        return None
    return raw


def _apply_supersedes(conn, source, ref, valid_from, supersedes):
    """Close the superseded doc's validity window — exactly once. Returns the pointer to store.

    ``supersedes`` names a ``ref`` in the SAME source. The older doc's ``valid_to`` is
    stamped with the new record's ``valid_from`` only while it is still open (NULL) — a
    second superseder never overwrites an existing close. Nothing is ever deleted; the
    closed doc stays fully retrievable via ``rag_query.py --include-superseded``.
    A missing target warns and carries on (the pointer still records intent); a
    self-supersede is ignored — a doc can't close its own window.
    """
    if not supersedes:
        return None
    target_ref = str(supersedes)
    if target_ref == ref:
        print(f"  ! {source}:{ref} declares it supersedes itself — ignored", file=sys.stderr)
        return None
    target_id = f"{source}:{target_ref}"
    row = conn.execute("SELECT valid_to FROM docs WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        print(
            f"  ! {source}:{ref} supersedes unknown doc {target_id!r} — nothing to close",
            file=sys.stderr,
        )
    elif row[0] is None:
        conn.execute(
            "UPDATE docs SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
            (valid_from, target_id),
        )
        conn.commit()
    return target_ref


def index_records(conn, records, cfg, *, dry_run=False, guard=None):
    """Embed and upsert records. **The provenance guard runs first, before anything is read.**

    This is the single write choke point every writer goes through (Dream's ``--ingest``,
    ``--local``, ``--chat``, `rag_projects.py`). ``guard`` is a
    `provenance_guard.ProvenanceGuard`; the default refuses anything whose writer cannot vouch for
    it (`../references/comms-mapping.md`). It is checked *before* ``rec["text"]`` is touched, so a
    refused record's payload is never even loaded into a local — the guard decides on ``source``
    and the producer stamp alone and never on content.

    Callers keep the historical ``(added, updated, skipped)`` return; refusals are on the guard
    object (and in the durable ledger), because a refusal is not a skip and folding it into one
    would hide it in a number that already means "unchanged text".
    """
    if guard is None:
        guard = pg.ProvenanceGuard()
    added = updated = skipped = 0
    for rec in records:
        if not guard.check(rec).allowed:
            continue
        source, ref, text = rec["source"], rec["ref"], rec["text"].strip()
        if not text:
            continue
        doc_id = f"{source}:{ref}"
        doc_hash = rc.content_hash(text)
        row = conn.execute(
            "SELECT hash, valid_from FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        now = _now()
        # Effective validity start: an explicit valid_from wins; an existing doc keeps its
        # stored start (re-embedding text doesn't change when the fact became true); a new
        # doc starts now (ingest time).
        valid_from = _norm_valid_from(rec) or (row[1] if row and row[1] else None) or now
        supersedes_ref = None
        if not dry_run:  # the supersede stamp is a write; dry-run touches nothing
            supersedes_ref = _apply_supersedes(
                conn, source, ref, valid_from, rec.get("supersedes")
            )
        if row and row[0] == doc_hash:
            # Unchanged text is never re-embedded, but a (re)declared supersedes pointer
            # is still recorded — the stamp above already ran (idempotent: only an open
            # valid_to is ever closed).
            if supersedes_ref:
                conn.execute(
                    "UPDATE docs SET supersedes = ? WHERE id = ?", (supersedes_ref, doc_id)
                )
                conn.commit()
            skipped += 1
            continue

        chunks = rc.chunk_text(text, int(cfg["CHUNK_CHARS"]), int(cfg["CHUNK_OVERLAP"]))
        if not chunks:
            continue
        if dry_run:
            if row:
                updated += 1
            else:
                added += 1
            continue

        vecs = rc.embed_texts(chunks, cfg=cfg)
        disposable, salience_cat = _norm_salience(rec)
        predicted_at = now if disposable else None
        # replace: drop any prior chunks for this doc, then insert fresh (chunk ids are
        # deterministic — doc_id#idx — so salience_access counters survive a re-embed)
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        # The lexical arm is deleted by the SAME key in the same breath, so the two indexes
        # cannot drift into disagreeing about which docs exist.
        rc.fts_delete_doc(conn, doc_id)
        for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
            conn.execute(
                "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at, "
                "disposable, salience_cat, predicted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{doc_id}#{i}", doc_id, source, ref, i, chunk, len(vec),
                 rc.pack_vec(vec), now, disposable, salience_cat, predicted_at),
            )
            rc.fts_index_chunk(conn, f"{doc_id}#{i}", doc_id, source, chunk)
        # valid_to is deliberately NOT in the upsert's update set: re-ingesting a
        # superseded doc's text never silently reopens its window (reopening would be a
        # new assertion — a fresh record). The supersedes pointer keeps its old value
        # unless this record declares a new one.
        conn.execute(
            "INSERT INTO docs (id, source, ref, hash, updated_at, valid_from, valid_to, "
            "supersedes) VALUES (?,?,?,?,?,?,NULL,?) "
            "ON CONFLICT(id) DO UPDATE SET hash=excluded.hash, updated_at=excluded.updated_at, "
            "valid_from=excluded.valid_from, "
            "supersedes=COALESCE(excluded.supersedes, supersedes)",
            (doc_id, source, ref, doc_hash, now, valid_from, supersedes_ref),
        )
        conn.commit()
        if row:
            updated += 1
        else:
            added += 1
    # Announce and persist the guard's verdicts here rather than in each caller: this function owns
    # the loop, so this is the one place no caller can forget. A dry run decides but writes nothing,
    # which is what makes `--dry-run` usable as "what WOULD be turned away tonight?".
    guard.report()
    if not dry_run:
        # Idempotent, and here as well as in main(): a caller that opened the index with plain
        # `rc.connect` (rag_projects.py, tests) must still get a durable ledger, not a failed write.
        pg.ensure_ledger(conn)
        guard.record(conn)
    return added, updated, skipped


def print_stats(conn):
    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"index: {docs} docs, {chunks} chunks")
    for source, c in conn.execute(
        "SELECT source, COUNT(*) FROM chunks GROUP BY source ORDER BY source"
    ):
        print(f"  {source}: {c} chunks")
    # salience-learning observability (SALIENCE_SETUP.md): tagged predictions + touched rows
    predicted, forgotten = (
        conn.execute("SELECT COUNT(*) FROM chunks WHERE disposable = 1").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM chunks WHERE disposable >= 2").fetchone()[0],
    )
    touched = conn.execute("SELECT COUNT(*) FROM salience_access").fetchone()[0]
    if predicted or forgotten or touched:
        print(f"salience: {predicted} predicted-disposable, {forgotten} approved-forgotten, "
              f"{touched} chunks in the access ledger")
    closed = conn.execute("SELECT COUNT(*) FROM docs WHERE valid_to IS NOT NULL").fetchone()[0]
    if closed:
        print(f"bitemporal: {closed} superseded/closed docs "
              f"(still retrievable via rag_query.py --include-superseded)")
    # The refusal ledger. Printed here so "what is being turned away, and since when?" is answerable
    # without writing SQL — a guard nobody can audit is no guard (`provenance_guard.py`).
    rows = pg.ledger_rows(conn)
    if rows:
        print("provenance refusals (references/comms-mapping.md):")
        for source, reason, overridden, count, first_at, last_at, last_ref in rows:
            verdict = "OVERRIDDEN — persisted" if overridden else "refused"
            print(f"  {source}/{reason}: {count} {verdict} · {first_at} → {last_at}"
                  + (f" · last ref {last_ref}" if last_ref else ""))


def stamp_dream_step(step: str, db_path=None) -> None:
    """Record that a Dream step actually ran (`dream_steps.py`), in the state dir holding the index.

    **Imported lazily and swallowed whole.** Bookkeeping that cannot import must never stop the
    work it was measuring. A failed stamp costs the row, never the run."""
    try:
        import dream_steps
        state = Path(db_path).resolve().parent if db_path else rc.STATE
        dream_steps.record(str(state), step)
    except Exception:  # noqa: BLE001 — see the docstring
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build/update the assistant's local RAG index.")
    ap.add_argument("--local", action="store_true", help="index run-log/carry-over")
    ap.add_argument("--chat", action="store_true",
                    help="index state/turns.jsonl as source='chat' — one document per turn, "
                         "both speakers, !private turns skipped")
    ap.add_argument("--ingest", nargs="+", metavar="JSONL", help="index records from JSONL file(s)")
    ap.add_argument("--rebuild", action="store_true", help="drop the whole index first")
    ap.add_argument("--stats", action="store_true", help="print index stats and exit")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true", help="report what would change; no embedding")
    ap.add_argument(pg.ALLOW_FLAG, action="append", default=[], metavar="SOURCE",
                    dest="allow_unattested",
                    help="persist a source the provenance guard would refuse (repeatable). The ONLY "
                         "override, and it lives on argv on purpose — see provenance_guard.py. "
                         "Every use is still counted in the refusal ledger.")
    args = ap.parse_args(argv)

    conn = rc.connect(args.db)
    try:  # close on every path — a lingering handle blocks file cleanup on Windows
        # The refusal ledger lives inside the index it is about; an index that predates it gains
        # the table here, on its next open, with no rebuild.
        pg.ensure_ledger(conn)
        # Commit the open itself: an additive migration on a legacy index (the bitemporal
        # backfill is an UPDATE) opens an implicit transaction, and a `--stats` run that never
        # writes would otherwise roll the new schema back on close.
        conn.commit()
        return _run(args, conn)
    finally:
        conn.close()


def _run(args, conn):
    if args.stats:
        print_stats(conn)
        return 0

    if args.rebuild and not args.dry_run:
        # NOTE: salience_access is deliberately NOT cleared — the index is a regenerable
        # cache, but the access ledger is accumulated *measurement*. Chunk ids are
        # deterministic (doc_id#idx), so counters stay valid across a rebuild. The
        # provenance_refusals ledger is kept for the same reason.
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM docs")
        rc.fts_clear(conn)
        conn.commit()
        print("rebuilt: cleared existing index (salience access ledger preserved)")

    # An index that predates the lexical arm gains `chunks_fts` on open, but gains it EMPTY — the
    # arm would then be silently inert until every doc happened to be re-ingested, which for a
    # stable doc is never. One `INSERT ... SELECT`, then a no-op forever after.
    if not args.dry_run:
        n = rc.fts_backfill(conn)
        if n:
            print(f"lexical arm: backfilled {n} existing chunks into the FTS index")

    records = []
    if args.local:
        records.extend(_iter_local())
    if args.chat:
        records.extend(_iter_chat_turns())
    if args.ingest:
        records.extend(_iter_jsonl(args.ingest))
    if not records:
        print("nothing to index (pass --local, --chat and/or --ingest)", file=sys.stderr)
        return 2

    guard = pg.ProvenanceGuard(allow_unattested=args.allow_unattested)
    try:
        added, updated, skipped = index_records(conn, records, rc.load_env(),
                                                dry_run=args.dry_run, guard=guard)
    except rc.OllamaError as exc:
        print(f"embedder unavailable — index not updated: {exc}", file=sys.stderr)
        return 3

    verb = "would index" if args.dry_run else "indexed"
    print(f"{verb}: +{added} new, ~{updated} updated, {skipped} unchanged")
    gs = guard.summary()
    if gs["refused"] or gs["overridden"]:
        print(f"provenance: {gs['refused']} refused, {gs['overridden']} persisted under override "
              f"({', '.join(f'{k}×{v}' for k, v in gs['refused_by_source'].items()) or 'none'})")
    if not args.dry_run:
        # Dream step 2b actually ran. Stamped HERE, by the script that did the work, rather than
        # by the Dream prompt that was supposed to remember — see `dream_steps.py`.
        stamp_dream_step("2b", args.db)
        print_stats(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
