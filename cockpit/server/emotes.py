"""`GET /api/emotes` — lists + serves `:shortcode:` emote images from a user-supplied directory
(`COCKPIT_EMOTE_DIR`). Feature is OFF (an empty list, no images served) when the env var is unset —
cockpit-spec.md's "Chat pane extra: custom emoji".
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

ALLOWED_EXTENSIONS = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".apng"}


def list_emotes(emote_dir: Optional[Path]) -> list[dict]:
    """[{"shortcode": "pog", "file": "pog.png"}, ...], sorted by shortcode. The shortcode is the
    filename stem, lowercased, so `:pog:` matches pog.png / Pog.PNG / POG.GIF alike (first one seen
    wins on a case-collision). Tolerant of a missing/unreadable directory — an empty list, never an
    error, matching every other read in this backend."""
    if emote_dir is None:
        return []
    try:
        paths = sorted(emote_dir.iterdir())
    except OSError:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        code = p.stem.lower()
        if code in seen:
            continue
        seen.add(code)
        out.append({"shortcode": code, "file": p.name})
    return out


def resolve_emote_file(emote_dir: Optional[Path], filename: str) -> Optional[Path]:
    """Resolve a requested emote filename to a real file strictly inside `emote_dir`, guarding against
    path traversal (the filename is a user-controlled URL path segment). Returns None (-> 404) for
    anything that doesn't resolve cleanly inside the directory, or doesn't exist."""
    if emote_dir is None or not filename:
        return None
    try:
        base = emote_dir.resolve()
        candidate = (emote_dir / filename).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate
