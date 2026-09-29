"""Shared helpers for the assistant's local semantic RAG index (Advisor Chain, phase B).

Free-but-local: embeddings via a local **Ollama** server (default model
``nomic-embed-text``), the index in stdlib **sqlite3** (vectors stored as
float32 blobs, brute-force cosine at query time). No third-party packages —
everything here is Python standard library, matching the repo convention.

This module is deliberately Notion-unaware: it embeds and stores *text records*
handed to it. The local prose (run-log / carry-over) and the chat thread
(``state/turns.jsonl``) the indexer reads directly; Notion-resident corpus (journal,
notes) is fetched by a Claude run that *has* the Notion MCP (Dream's nightly refresh)
and passed in via ``--ingest``.

The RAG index is **additive** — callers fall back to Notion-search whenever Ollama
or the index is unavailable (see ``rag_query.py``). See ``RAG_SETUP.md``.

Docs are **bitemporal**: each carries a validity interval (``valid_from`` /
``valid_to``) plus an optional ``supersedes`` pointer, so queries answer *what is
true now* instead of *what is similar* — superseded facts stay fully retrievable,
flagged, never deleted (see :func:`_migrate_bitemporal` and ``RAG_SETUP.md``).

Retrieval is **hybrid**: an FTS5 lexical arm (:func:`_migrate_fts`) sits beside the
dense vectors so a rare proper noun the embedder buries is still findable. The
provenance refusal ledger (``provenance_refusals``) lives in the same file but is
owned by ``provenance_guard.py`` — ``rag_index.py`` creates it on open.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE.parent / "state"
DEFAULT_DB = STATE / "rag-index.sqlite"
ENV_FILE = HERE / "rag.env"

DEFAULTS = {
    "OLLAMA_URL": "http://localhost:11434",
    "EMBED_MODEL": "nomic-embed-text",
    "CHUNK_CHARS": "800",
    "CHUNK_OVERLAP": "150",
}

# --------------------------------------------------------------- salience taxonomy
# Closed vocabulary for salience-learning tags (see ../references/salience.md and
# SALIENCE_SETUP.md). Closed like router.py's TRIVIAL_CATEGORIES: an unknown tag is
# coerced to "unknown" (the safe bucket), never passed through raw.
SALIENCE_CATEGORIES = {
    "ephemeral.ack",        # "took my meds", "did that" — near-certain disposable
    "logistics.transient",  # one-off scheduling detail now past ("moved 3pm to 4")
    "status.snapshot",      # point-in-time state a later query supersedes
    "identity.core",        # birthdays, names, relationships — NEVER disposable
    "commitment.durable",   # a promise/deadline that matters until resolved
    "preference.standing",  # "I shower at night" — proposed-learnings material
    "unknown",              # the safe default when Dream can't classify
}
# Only these categories may carry a disposable=1 prediction; a prediction on any
# other category is cleared at index time (identity.core can never be predicted away).
DISPOSABLE_ELIGIBLE = {"ephemeral.ack", "logistics.transient", "status.snapshot"}
# chunks.disposable ladder:
#   0 = normal · 1 = predicted-disposable (a logged prediction — the row stays FULLY
#   retrievable; observe-only) · 2 = approved-forgotten (soft prune, set only on
#   the owner's explicit approval — excluded from answers, still counted for un-forget
#   evidence). Nothing in this codebase writes 2 automatically.


class OllamaError(RuntimeError):
    """Raised when the local embedder is unreachable or returns junk."""


def load_env(env_file: Path | str | None = ENV_FILE) -> dict:
    """Config = DEFAULTS, overridden by rag.env (if present), then the process env."""
    cfg = dict(DEFAULTS)
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip()
    for key in list(cfg):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


# ---------------------------------------------------------------- embeddings

# How big one ``/api/embed`` request may get. TWO bounds, because the failure has two shapes.
#
# **Without them a real ingest reads as "embedder down".** ``/api/embed`` takes a list, and
# `rag_index.index_records` embeds one DOCUMENT per call — so a long-lived `state/run-log.md`
# (hundreds of thousands of chars → hundreds of chunks) becomes one enormous request. Ollama
# answers **HTTP 400**, this module turns that into `OllamaError`, `rag_index.py` exits 3 as
# "embedder unavailable", and Dream step 2b skips silently because the index is a regenerable
# cache. **A transport bug wearing the costume of a healthy graceful degradation**: nothing
# crashes, nothing alarms, the index just stops moving — while small-batch callers
# (`rag_projects.py --ingest`) keep succeeding against the same server.
#
# **What the 400 actually is — and it is NOT a payload-size limit.** The response body reads
# ``Post "http://127.0.0.1:<port>/tokenize": dial tcp ... actively refused it``. Ollama's
# front-end opens **one internal connection per input item** to its own llama-server runner; the
# runner stays alive throughout, so this is a *connection* failure against a live listener — a big
# batch fires hundreds of loopback connects back-to-back and overflows the listen backlog. **Item
# count is the driver, not bytes**, and the failure is PROBABILISTIC rather than a clean cliff —
# which is why a single-sample "n=400 passed, n=800 failed" reading looks like a size limit and
# isn't.
#
# Measured with repeat trials (one sample cannot see a probability): single requests of 64 items
# always passed, 256 usually, 448 rarely; full passes over ~850 real chunks succeeded every time at
# item caps of 128, 64 and 32, while a byte-budget-only split (~300 items/request) still failed
# intermittently. So the cap is **64 — half the largest value measured clean**, and it is nearly
# free: latency is dominated by the embedding itself, not the request count, so buying reliability
# with more, smaller requests costs well under a second per ingest.
#
# The byte budget stays as the SECOND bound, and it is not redundant: it is what keeps a handful of
# very long chunks (a session distillation against a one-line chat turn — the sizes here vary by
# more than an order of magnitude) from assembling a huge body under a small item count. Two bounds
# because there are two ways to be too big, and whichever binds first wins.
EMBED_PAYLOAD_BUDGET_BYTES = 256 * 1024
EMBED_MAX_BATCH_ITEMS = 64


def _embed_slices(texts, budget: int | None = None, max_items: int | None = None):
    """Partition ``texts`` into consecutive runs under BOTH the byte budget and the item cap.

    **Order-preserving and total**: every input lands in exactly one slice, in its original
    position, so ``sum(len(s) for s in slices) == len(texts)`` always. That is the invariant the
    caller's ``len(result) == len(texts)`` rests on, and a short result would corrupt the index
    (chunk i paired with vector j) rather than merely skip it — strictly worse than the failure
    these bounds exist to fix.

    **An item bigger than the whole budget gets its own slice and is still sent.** The bounds exist
    to keep a *batch* small; neither is ever allowed to be the thing that discards an input. If
    Ollama then rejects that lone chunk, that is a genuine embedder failure and surfaces as one.

    Sized with ``json.dumps`` rather than ``len(text)`` because escaping is not free — a markdown
    chunk full of quotes and newlines encodes above its character count. The ``+ 2`` is
    ``json.dumps``'s actual item separator (``", "``), which makes the sum of the costs exactly
    ``len(json.dumps(slice))``; the brackets and the envelope (``model``, the key names) are tens of
    bytes against a 256 kB budget and are left in the headroom.
    """
    # Resolved at CALL time, not bound as defaults: the module globals are what a test lowers to
    # exercise the split path, and default arguments would freeze them at import.
    budget = EMBED_PAYLOAD_BUDGET_BYTES if budget is None else budget
    max_items = EMBED_MAX_BATCH_ITEMS if max_items is None else max_items
    slices, current, size = [], [], 0
    for text in texts:
        cost = len(json.dumps(text).encode("utf-8")) + 2
        if current and (size + cost > budget or len(current) >= max_items):
            slices.append(current)
            current, size = [], 0
        current.append(text)
        size += cost
    if current:
        slices.append(current)
    return slices


def _post_embed(url: str, payload: dict, timeout):
    """POST one ``/api/embed`` request and return the decoded JSON.

    Its own function purely as a **seam**: `test_rag_common.py` substitutes a fake transport here
    to assert how many requests a batch became and how it was partitioned — the thing that touches
    the outside world is one replaceable call.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def embed_texts(texts, cfg=None, timeout=120):
    """Return one float vector per input text via Ollama's ``/api/embed``.

    Issues **one request per sub-batch** (see :data:`EMBED_PAYLOAD_BUDGET_BYTES` and
    :data:`EMBED_MAX_BATCH_ITEMS`) and concatenates the results, so the returned list is always
    ``len(texts)`` long and in the input's order.

    Raises :class:`OllamaError` if the server is unreachable, any sub-batch returns an unexpected
    shape, or two sub-batches disagree about the vector width — callers treat that as "no semantic
    layer, fall back". **Never returns a short or partial list**: a failure anywhere raises, because
    the caller zips this against its chunks and a silently-short result would mis-pair every vector
    after the gap.
    """
    texts = [t for t in texts]
    if not texts:
        return []
    cfg = cfg or load_env()
    url = cfg["OLLAMA_URL"].rstrip("/") + "/api/embed"
    out, dim = [], None
    for batch in _embed_slices(texts):
        try:
            data = _post_embed(url, {"model": cfg["EMBED_MODEL"], "input": batch}, timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise OllamaError(f"embedding request to {url} failed: {exc}") from exc
        embs = data.get("embeddings") if isinstance(data, dict) else None
        # Validated per sub-batch, not once: a server that answers the first slice correctly and
        # the fourth with junk is precisely the failure a single up-front check would wave through.
        if not isinstance(embs, list) or len(embs) != len(batch) or not embs[0]:
            raise OllamaError("unexpected embedding response shape from Ollama")
        for vec in embs:
            if not vec:
                raise OllamaError("unexpected embedding response shape from Ollama")
            if dim is None:
                dim = len(vec)
            elif len(vec) != dim:
                # `chunks.dim` is one column for the whole table and `unpack_vec` is handed that
                # single width, so a ragged batch would be packed and read back as garbage. Refuse
                # rather than store it.
                raise OllamaError(
                    f"embedding width changed mid-batch ({dim} -> {len(vec)}) — refusing to index")
        out.extend(embs)
    return out


# ------------------------------------------------------------ vector packing

def pack_vec(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vec(blob: bytes, dim: int):
    return list(struct.unpack(f"<{dim}f", blob))


def cosine(a, b) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ------------------------------------------------------------------- sqlite

def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docs (
            id         TEXT PRIMARY KEY,   -- source:ref
            source     TEXT NOT NULL,
            ref        TEXT NOT NULL,
            hash       TEXT NOT NULL,      -- sha1 of the doc text; skip re-embed if unchanged
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id         TEXT PRIMARY KEY,   -- source:ref#idx
            doc_id     TEXT NOT NULL,
            source     TEXT NOT NULL,
            ref        TEXT NOT NULL,
            idx        INTEGER NOT NULL,
            text       TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vec        BLOB NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
    _migrate_salience(conn)
    _migrate_bitemporal(conn)
    _migrate_fts(conn)
    return conn


def _migrate_fts(conn: sqlite3.Connection) -> bool:
    """The lexical arm — an FTS5 index over the same chunks. Additive + idempotent.

    **Why a second index at all.** Dense embeddings place a rare proper noun near *the token*, not
    near the events attached to it. Asked for a bare name that appears in only one or two chunks,
    the dense arm can rank the right chunk dozens of places down — below the salience access floor,
    in a corpus that contains it — while a content query about the same story ranks it first. Worse,
    what such a query tends to return instead is the turns in which the name was *said* and nothing
    was known, so a chat index with only a dense arm, asked "who is X?", retrieves the assistant's
    own ignorance and returns it confidently. That is worse than no answer.

    **No new dependency.** FTS5 is compiled into the SQLite bundled with CPython, so the sanctioned
    dependency set is untouched.

    **A plain content-bearing table, not `content=''` and not external-content.** Both alternatives
    fail on the delete paths below:

    * *Contentless* (`content=''`) stores no column values: `SELECT chunk_id` returns NULL and
      `DELETE ... WHERE chunk_id = ?` matches nothing.
    * *External content* (`content='chunks'`) would avoid duplicating the text, but its deletes
      require the row to still exist in `chunks` at delete time — an ordering constraint between
      two `DELETE`s in another module, which is exactly the kind of coupling that rots silently.

    So the text is stored twice. Chunk text is a small fraction of the file (the vectors, at 768
    floats each, dominate), so the duplicate is the cheapest thing in it.

    **Fail-soft.** A SQLite built without FTS5 raises here and returns False; the caller keeps a
    working dense index and simply has no lexical arm. A retrieval system that refuses to start
    because half of it is missing is worse than one that degrades."""
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                doc_id   UNINDEXED,
                source   UNINDEXED,
                text,
                tokenize = "unicode61 remove_diacritics 2"
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def fts_available(conn: sqlite3.Connection) -> bool:
    """Whether this database HAS the lexical arm — checked rather than assumed, because an index
    built before the arm existed has no `chunks_fts` and must keep answering from the dense arm."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def fts_index_chunk(conn: sqlite3.Connection, chunk_id: str, doc_id: str, source: str,
                    text: str) -> None:
    """Mirror one chunk into the lexical arm.

    Idempotent on its own: the prior row for this `chunk_id` is dropped first, so re-indexing a
    doc cannot accumulate duplicates even if the caller skipped `fts_delete_doc`. Chunk ids are
    deterministic (`doc_id#idx`), so this is the same identity the dense side uses."""
    if not fts_available(conn):
        return
    conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, doc_id, source, text) VALUES (?, ?, ?, ?)",
        (chunk_id, doc_id, source, text),
    )


def fts_delete_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    """Drop a doc's lexical rows. Keyed on `doc_id` because that is what the dense side's
    per-doc replace is keyed on (`DELETE FROM chunks WHERE doc_id = ?`) — the two indexes are
    deleted by the same key so they cannot drift into disagreeing about what exists."""
    if not fts_available(conn):
        return
    conn.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))


def fts_clear(conn: sqlite3.Connection) -> None:
    """Empty the lexical arm — the `--rebuild` path's counterpart to `DELETE FROM chunks`."""
    if not fts_available(conn):
        return
    conn.execute("DELETE FROM chunks_fts")


_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z_À-ɏ]+")


def fts_backfill(conn: sqlite3.Connection) -> int:
    """One-shot: copy every existing chunk into an EMPTY lexical arm. Returns rows written.

    **Why this exists.** `_migrate_fts` is additive, so an older index gains `chunks_fts` the
    moment it is opened — but gains it *empty*. Without a backfill the lexical arm would then be
    silently inert until every doc happened to be re-ingested, which for a stable doc is never.
    An index that reports a working lexical arm and returns nothing from it is the worst of the
    three states, so this closes it without anyone having to remember.

    **Runs in the indexer, not in `connect()`** — bulk work does not belong on a path every query
    pays for — and it is a SINGLE `INSERT ... SELECT`, so an interrupted run leaves the table
    empty rather than half-filled. A half-filled table would defeat the emptiness test below and
    make the gap permanent and invisible.

    A no-op once the arm has rows, so the nightly caller pays one statement forever after."""
    if not fts_available(conn):
        return 0
    try:
        if conn.execute("SELECT 1 FROM chunks_fts LIMIT 1").fetchone():
            return 0
        cur = conn.execute(
            "INSERT INTO chunks_fts (chunk_id, doc_id, source, text) "
            "SELECT id, doc_id, source, text FROM chunks")
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error:
        return 0  # additive: a failed backfill costs recall, never the ingest


def fts_match_query(query_text: str) -> str | None:
    """Turn free text into a SAFE FTS5 MATCH expression, or None when there is nothing to match.

    **Never interpolate user text into MATCH.** FTS5's query language gives `"`, `*`, `:`, `^`,
    `-`, `(`, `)`, `AND`/`OR`/`NOT` and `NEAR` their own meanings, so a raw question mark or an
    apostrophe is a syntax error and a bare `NOT` silently inverts the query. Every token is
    therefore extracted and re-emitted as a quoted phrase literal, which has no syntax left in it.

    **Escaping only — the rarity cut lives in `fts_rare_terms`.** This function makes text SAFE;
    it does not decide what is worth matching. Splitting them keeps the escaping testable without
    a corpus."""
    if not isinstance(query_text, str):
        return None
    toks = [t for t in _FTS_TOKEN_RE.findall(query_text) if len(t) > 1]
    if not toks:
        return None
    return " OR ".join('"%s"' % t for t in toks[:_FTS_MAX_TERMS])


_FTS_MAX_TERMS = 32


# A token in more than this fraction of the corpus is not what the lexical arm is for. On a real
# conversational index, function words saturate it (`is` near half of all chunks, `the` in most),
# while the arm's actual targets — the rare proper nouns the dense side buries — sit well under 1%.
# 1% rather than 2% because question words (`who`, `what`) land between the two on a real corpus,
# and at 2% `who` survived and promoted a weaker chunk over a better dense hit on exactly the
# "who is X?" query this arm exists for. A first cut of this feature had no such filter, on the
# reasoning that bm25's IDF would discount common terms by itself. It does not: bm25 ranks WITHIN
# the matching set, and ORing every token makes that set most of the corpus, so a short chunk
# stuffed with `who` and `is` outranks a genuinely better dense hit. That regression is what this
# constant exists to prevent, and `test_rag_fts.py::RarityCut` is what keeps it prevented.
FTS_MAX_DOC_FRACTION = 0.01

# …but a percentage alone would silence the arm entirely on a young index: at 20 chunks, 1% is
# 0.2, so a token in ONE chunk already exceeds it. A token appearing in a handful of documents is
# rare by any measure, whatever the corpus size, so this floor wins whenever it is the larger.
FTS_ALWAYS_RARE_DOCS = 3


def fts_rare_terms(conn: sqlite3.Connection, query_text: str) -> list[str]:
    """The query's tokens, keeping only those RARE enough to be worth a lexical match.

    The lexical arm exists for tokens the dense arm cannot find — a rare proper noun it places
    near *the token* rather than near the events attached to it. A token the corpus is saturated
    with is the opposite case: the dense arm has abundant semantic context for it and is already
    good at it, so matching it lexically adds noise and nothing else.

    This is a stopword list DERIVED FROM THE CORPUS rather than written by hand, which is the
    point — it needs no maintenance, it adapts as the corpus grows, and it is right about *this*
    corpus rather than about English in general. A project codename that would be a rare word
    anywhere else can be a frequent topic in one owner's index, and there the dense arm should
    own it.

    Returns `[]` — never raises — when the arm is absent or nothing survives the cut."""
    if not fts_available(conn):
        return []
    if not isinstance(query_text, str):
        return []
    toks, seen = [], set()
    for t in _FTS_TOKEN_RE.findall(query_text):
        low = t.lower()
        if len(t) > 1 and low not in seen:
            seen.add(low)
            toks.append(t)
        if len(toks) >= _FTS_MAX_TERMS:
            break
    if not toks:
        return []
    try:
        total, = conn.execute("SELECT count(*) FROM chunks_fts").fetchone()
    except sqlite3.Error:
        return []
    if not total:
        return []
    ceiling = max(FTS_ALWAYS_RARE_DOCS, total * FTS_MAX_DOC_FRACTION)
    keep = []
    for t in toks:
        try:
            n, = conn.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                ('"%s"' % t,)).fetchone()
        except sqlite3.Error:
            continue
        # n == 0 is dropped too: a token the corpus has never seen can only add MATCH terms —
        # a lexical index cannot return a string that was never written, so the arm correctly
        # contributes nothing.
        if 0 < n <= ceiling:
            keep.append(t)
    return keep


def fts_search(conn: sqlite3.Connection, query_text: str, *, limit: int = 20,
               source: str | None = None) -> list[str]:
    """Chunk ids matching `query_text` lexically, best (lowest bm25) first.

    Only tokens RARE in this corpus are matched (`fts_rare_terms`) — matching the common ones
    made queries measurably worse, which is the whole reason that filter exists.

    Returns `[]` — never raises — when the arm is absent, no token is rare enough to be worth
    matching, or SQLite dislikes the expression. The lexical arm is strictly ADDITIVE: it may only
    reorder candidates the dense arm already scored, so every failure here degrades to the
    dense-only behaviour."""
    if not fts_available(conn):
        return []
    terms = fts_rare_terms(conn, query_text)
    if not terms:
        return []
    match = fts_match_query(" ".join(terms))
    if not match:
        return []
    sql = "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?"
    params: tuple = (match,)
    if source:
        sql += " AND source = ?"
        params += (source,)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params += (int(limit),)
    try:
        return [r[0] for r in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def _migrate_salience(conn: sqlite3.Connection) -> None:
    """Salience-learning schema — additive + idempotent, so a live index upgrades in place.

    sqlite has no ``ADD COLUMN IF NOT EXISTS``; guard on ``PRAGMA table_info`` instead so a
    re-run never throws "duplicate column". Lives inside :func:`connect` so every caller
    (index, query, rollup, tests) sees the upgraded schema for free.
    See ``../references/salience.md`` + ``SALIENCE_SETUP.md``.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "disposable" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN disposable INTEGER NOT NULL DEFAULT 0")
    if "salience_cat" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN salience_cat TEXT")
    if "predicted_at" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN predicted_at TEXT")
    # Sidecar access ledger — one aggregated row per touched chunk (an upsert counter, NOT a
    # per-touch event log, so it stays bounded; mirrors how acks.json is a keyed ledger).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS salience_access (
            chunk_id     TEXT PRIMARY KEY REFERENCES chunks(id),
            doc_id       TEXT NOT NULL,
            salience_cat TEXT,
            hit_count    INTEGER NOT NULL DEFAULT 0,   -- times returned in top-k above the floor
            first_hit_at TEXT,
            last_hit_at  TEXT,
            max_score    REAL NOT NULL DEFAULT 0.0     -- best cosine ever seen (near-miss ≠ strong recall)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_cat ON salience_access(salience_cat)")


def _migrate_bitemporal(conn: sqlite3.Connection) -> None:
    """Bitemporal validity schema on ``docs`` — additive + idempotent, like the salience one.

    Three columns turn every doc into a validity interval, so queries can answer *what is
    true NOW* rather than *what is similar* — an agent that finds three versions of a fact and
    uses the stale one is the coherence bug this exists to kill:

    * ``valid_from``  — when the fact became true (ingest time unless declared). NOT NULL;
      sqlite's ``ADD COLUMN`` can't run a non-constant default, so legacy rows are added
      with ``''`` then backfilled from ``updated_at`` — the closest thing to ingest time a
      pre-bitemporal row carries.
    * ``valid_to``    — when it stopped being true. NULL = still current.
    * ``supersedes``  — the ``ref`` (same source) of the doc this one replaced.

    Same posture as salience's disposable ladder: a superseded doc is never deleted or
    hidden-forever — it leaves default answers but stays fully retrievable, flagged, via
    ``rag_query.py --include-superseded`` / ``--as-of``. No re-embedding is ever required.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
    if "valid_from" not in have:
        conn.execute("ALTER TABLE docs ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE docs SET valid_from = updated_at WHERE valid_from = ''")
    if "valid_to" not in have:
        conn.execute("ALTER TABLE docs ADD COLUMN valid_to TEXT")
    if "supersedes" not in have:
        conn.execute("ALTER TABLE docs ADD COLUMN supersedes TEXT")


# ----------------------------------------------------------- bitemporal time

def parse_ts(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime; ``None`` when it can't.

    Fail-open by design: callers treat ``None`` as "no constraint", so a garbage
    timestamp can cost precision but can never hide a memory. Accepts a trailing
    ``Z`` and assumes UTC for naive stamps (the repo's timestamps are UTC-aware).
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def doc_is_current(valid_from, valid_to, as_of: datetime | None = None) -> bool:
    """Is a doc's validity interval live?

    * ``as_of=None`` — the **"what is true NOW"** default: a doc is current unless its
      ``valid_to`` parses to a real timestamp ≤ now. A future ``valid_to`` (a known
      expiry that hasn't hit) is still current; an open/garbage one fails open to
      current — never hide on bad data.
    * ``as_of=<aware datetime>`` — time travel: current means
      ``valid_from ≤ as_of < valid_to`` (open-ended when ``valid_to`` is NULL); the
      interval is half-open, so at the instant of supersession the successor is
      current and the predecessor is not. An unparsable ``valid_from`` fails open.
    """
    to_dt = parse_ts(valid_to)
    if as_of is None:
        return to_dt is None or to_dt > datetime.now(timezone.utc)
    from_dt = parse_ts(valid_from)
    if from_dt is not None and from_dt > as_of:
        return False
    return to_dt is None or to_dt > as_of


# ------------------------------------------------------------------ chunking

def chunk_text(text: str, size: int, overlap: int):
    """Split prose into ~``size``-char chunks, breaking on whitespace, with overlap."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:  # prefer a natural break in the back half of the window
            brk = text.rfind("\n", start, end)
            if brk <= start + size // 2:
                brk = text.rfind(" ", start, end)
            if brk > start + size // 2:
                end = brk
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()
