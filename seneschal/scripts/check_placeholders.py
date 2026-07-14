#!/usr/bin/env python3
"""CI guard: no real UUID-shaped identifier may be committed to this repo.

Seneschal's store registries and docs use *placeholder* ids that the setup flow
replaces locally (the filled copies are gitignored). This check fails if any
tracked text file contains a UUID that is neither a sanctioned placeholder nor
explicitly allowlisted below.

Sanctioned placeholder scheme: the first four groups are all zeros —
``00000000-0000-0000-0000-<any 12 hex>`` (or its 32-char compact form). Docs
should use ``00000000-0000-0000-0000-000000000000``, ``…-000000000001``, etc.

Run from the repo root:  python seneschal/scripts/check_placeholders.py
"""
import re
import subprocess
import sys
from pathlib import Path

# Real, non-placeholder UUIDs that are intentionally public (none yet). Add the
# lowercase hyphenated form plus a comment saying why it is safe to publish.
ALLOWLIST: set[str] = set()

# Binary or generated files that legitimately contain arbitrary hex runs.
SKIP_SUFFIXES = {".jar", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".webp"}

HYPHENATED = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
COMPACT = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
PLACEHOLDER = re.compile(r"^0{8}-?0{4}-?0{4}-?0{4}-?[0-9a-f]{12}$")


def normalize(u: str) -> str:
    u = u.lower()
    if "-" not in u:
        u = f"{u[0:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:32]}"
    return u


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    )
    bad = []
    for rel in out.stdout.decode("utf-8", "replace").split("\0"):
        if not rel or Path(rel).suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in HYPHENATED.findall(line) + COMPACT.findall(line):
                u = match.lower()
                if PLACEHOLDER.match(u) or normalize(u) in ALLOWLIST:
                    continue
                bad.append(f"{rel}:{lineno}: {match}")
    if bad:
        print("Non-placeholder UUIDs found (use 00000000-0000-0000-0000-… placeholders,")
        print("or add a justified entry to ALLOWLIST in check_placeholders.py):")
        for b in bad:
            print(f"  {b}")
        return 1
    print("placeholder check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
