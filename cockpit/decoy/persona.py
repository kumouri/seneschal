"""The decoy steward's system prompt — assembled from the tracked persona doc + easter-egg bank.

`receptionist.md` and `eggs.md` are both tracked Markdown (the fun is reviewable, per
`seneschal/docs/cockpit-spec.md`'s "The decoy" section) — this module just glues them into the one
system-prompt string `server.py` sends to Ollama on every turn. No state, no network, pure text
assembly; trivially unit-testable, and re-read fresh on every call so an edit to either file takes
effect on the next request without a restart (these are tiny local files — the read cost is
negligible next to an LLM call).
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
RECEPTIONIST_FILE = HERE / "receptionist.md"
EGGS_FILE = HERE / "eggs.md"

# Verbatim substrings from receptionist.md's "What you can never do" section. Kept here (not
# duplicated in test_decoy.py) so the test that asserts the built prompt states its isolation
# guarantees is checking against the SAME markers this module promises, not a second copy that could
# silently drift if receptionist.md is reworded.
ISOLATION_MARKERS = (
    "no tools",
    "no access to the owner's real data",
    "cannot take any real action",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_system_prompt() -> str:
    """The final system prompt sent with every `/api/chat` turn: the persona doc, then the
    easter-egg bank as a second section. Missing files degrade to an empty section rather than
    raising — a typo'd path should never turn into a 500 on a public chat endpoint."""
    persona = _read(RECEPTIONIST_FILE).strip()
    eggs = _read(EGGS_FILE).strip()
    parts = [part for part in (persona, eggs) if part]
    return "\n\n---\n\n".join(parts)
