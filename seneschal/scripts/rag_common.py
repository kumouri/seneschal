"""Shared helpers for the assistant's local semantic RAG index (Advisor Chain, phase B).

Free-but-local: embeddings via a local **Ollama** server (default model
``nomic-embed-text``), the index in stdlib **sqlite3** (vectors stored as
float32 blobs, brute-force cosine at query time). No third-party packages —
everything here is Python standard library, matching the repo convention.

This module is deliberately Notion-unaware: it embeds and stores *text records*
handed to it. The local prose (run-log / carry-over / context-digest) it can read
directly; Notion-resident corpus (journal, notes) is fetched by a Claude run that
*has* the Notion MCP (Dream's nightly refresh) and passed in via ``--ingest``.

The RAG index is **additive** — callers fall back to Notion-search whenever Ollama
or the index is unavailable (see ``rag_query.py``). See ``RAG_SETUP.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import struct
import urllib.error
import urllib.request
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

def embed_texts(texts, cfg=None, timeout=120):
    """Return one float vector per input text via Ollama's ``/api/embed``.

    Raises :class:`OllamaError` if the server is unreachable or the response
    shape is unexpected — callers treat that as "no semantic layer, fall back".
    """
    texts = [t for t in texts]
    if not texts:
        return []
    cfg = cfg or load_env()
    url = cfg["OLLAMA_URL"].rstrip("/") + "/api/embed"
    body = json.dumps({"model": cfg["EMBED_MODEL"], "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise OllamaError(f"embedding request to {url} failed: {exc}") from exc
    embs = data.get("embeddings")
    if not isinstance(embs, list) or len(embs) != len(texts) or not embs[0]:
        raise OllamaError("unexpected embedding response shape from Ollama")
    return embs


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
    return conn


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
