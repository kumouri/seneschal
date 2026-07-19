#!/usr/bin/env python3
"""Project-state layer for the assistant's local semantic RAG index (Retrieval, phase B).

Scans the owner's project trees — git repos under configured roots, explicitly
listed strays (e.g. a project embedded inside an app's install dir), and
unversioned top-level project dirs — plus the owner's GitHub repo list (via ``gh``),
and turns each project into one **state summary document**: where it lives,
what it is, which branch it's on, how dirty it is, what changed recently, and
what the README says it does. The docs are written to
``state/projects.jsonl`` (source ``"project"``), rendered human-readably to
``state/projects-map.md`` (the "where do I go look" file), and — with
``--ingest`` — embedded into ``state/rag-index.sqlite`` via ``rag_index`` so
"what's the state of <project>?" is answerable by semantic recall.

Like the rest of the RAG layer it is **free-but-local and additive**: stdlib
only, embeddings via local Ollama (graceful exit 3 when it's down — the JSONL
and the map are still written), and never a hard dependency for anything.

Freshness: doc ids are ``project:<path>`` and indexing is incremental by text
hash, so re-running re-embeds only projects whose state actually changed.
After an ingest the ``project`` source is **reconciled**: docs for projects
that vanished from the scan (moved/deleted) are dropped from the index
(``--no-prune`` disables; the salience access ledger is measurement and is
never touched). Dream re-runs this nightly right after the journal refresh
(see ``RAG_SETUP.md`` + the Dream section of ``seneschal/SKILL.md``).

Config — ``state/project-roots.json`` (gitignored; seed
``project-roots.example.json``)::

    {
      "schema": "seneschal.projects/1",
      "roots": ["C:/Users/<you>/workspace"],       // scanned for git repos
      "non_git_roots": ["C:/Users/<you>/workspace"],  // immediate non-git subdirs summarized too
      "extras": ["D:/path/to/a/stray/project"],    // explicit strays, git or not
      "max_depth": 3,
      "exclude_names": ["node_modules", ".venv", "venv", "__pycache__", "dist", "target"],
      "github": {"enabled": true, "limit": 200}
    }

Examples
--------
    python rag_projects.py                  # scan → projects.jsonl + projects-map.md
    python rag_projects.py --ingest         # …and embed into the RAG index
    python rag_projects.py --dry-run        # list what would be scanned
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import rag_common as rc
import rag_index as ri

CONFIG_PATH = rc.STATE / "project-roots.json"
JSONL_PATH = rc.STATE / "projects.jsonl"
MAP_PATH = rc.STATE / "projects-map.md"
SOURCE = "project"

GIT_TIMEOUT = 20        # seconds per git command; a hung repo degrades, never blocks the run
README_CHARS = 1200     # how much README each summary carries
COMMIT_COUNT = 5

DEFAULT_EXCLUDES = {
    "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "dist", "build", "target", "out", ".gradle", ".next", "bin", "obj",
}


# ------------------------------------------------------------------- config

def load_config(path: Path | str = CONFIG_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"no config at {path} — copy project-roots.example.json beside it and "
            "fill in your roots (see RAG_SETUP.md)"
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("roots", [])
    cfg.setdefault("non_git_roots", [])
    cfg.setdefault("extras", [])
    cfg.setdefault("max_depth", 3)
    cfg.setdefault("exclude_names", sorted(DEFAULT_EXCLUDES))
    cfg.setdefault("github", {})
    cfg["github"].setdefault("enabled", True)
    cfg["github"].setdefault("limit", 200)
    return cfg


# ---------------------------------------------------------------- discovery

def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()  # dir (normal) or file (worktree/submodule)


def find_repos(root: Path, max_depth: int, exclude_names) -> list[Path]:
    """Git repos under ``root``, pruned: never descends past a found repo or
    into excluded/hidden dirs — so nested worktrees/submodules don't double-count."""
    exclude = set(exclude_names)
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if _is_repo(d):
            found.append(d)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name in exclude or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    if root.exists():
        walk(root, 0)
    return found


# --------------------------------------------------------------------- git

def git(repo: Path, *args: str) -> str | None:
    """One git read in ``repo`` (fsmonitor off — it hangs in GitKraken-touched
    repos here). Returns stdout, or None on any failure/timeout."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def normalize_remote(url: str) -> str | None:
    """``git@github.com:kumouri/x.git`` and ``https://github.com/kumouri/x/``
    both → ``github.com/kumouri/x`` — the key GitHub matching joins on."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("git@"):  # git@host:owner/repo(.git)
        url = url[4:].replace(":", "/", 1)
    for scheme in ("https://", "http://", "ssh://git@", "ssh://"):
        if url.startswith(scheme):
            url = url[len(scheme):]
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower() or None


def _readme_excerpt(path: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = path / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                return text[:README_CHARS]
    return ""


def _manifest_description(path: Path) -> str:
    """A one-liner from package.json / pyproject.toml when the README is thin."""
    pkg = path / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            desc = data.get("description") or ""
            if desc:
                return str(desc)
        except (OSError, ValueError):
            pass
    pyproject = path / "pyproject.toml"
    if pyproject.is_file():  # cheap line-scan; a TOML parser is overkill for one key
        try:
            for line in pyproject.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("description") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def summarize_repo(path: Path, gh_by_key: dict | None = None) -> dict:
    """One project-state summary dict for a git repo (degrades field-by-field)."""
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    remote = git(path, "remote", "get-url", "origin")
    status = git(path, "status", "--porcelain")
    dirty = len(status.splitlines()) if status else 0
    log = git(path, "log", f"-{COMMIT_COUNT}", "--format=%as %s")
    commits = log.splitlines() if log else []
    key = normalize_remote(remote) if remote else None
    gh = (gh_by_key or {}).get(key)
    return {
        "kind": "repo",
        "name": path.name,
        "path": str(path),
        "branch": branch,
        "dirty_files": dirty,
        "remote": remote,
        "github": gh,        # the matched `gh repo list` entry, if any
        "commits": commits,  # newest first: "YYYY-MM-DD subject"
        "readme": _readme_excerpt(path),
        "description": (gh or {}).get("description") or _manifest_description(path),
    }


def summarize_plain_dir(path: Path) -> dict:
    """A light summary for an unversioned project dir (no git to interrogate)."""
    try:
        entries = sorted(p.name for p in path.iterdir())[:30]
    except OSError:
        entries = []
    return {
        "kind": "dir",
        "name": path.name,
        "path": str(path),
        "entries": entries,
        "readme": _readme_excerpt(path),
        "description": _manifest_description(path),
    }


# ------------------------------------------------------------------- github

def github_repos(limit: int = 200, runner=subprocess.run) -> list[dict]:
    """The owner's repo list via ``gh`` (already authed on this host). [] on any failure —
    the GitHub layer is additive, exactly like the embedder."""
    fields = "name,owner,description,isPrivate,isArchived,pushedAt,primaryLanguage,url"
    try:
        proc = runner(
            ["gh", "repo", "list", "--limit", str(limit), "--json", fields],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return []
    out = []
    for r in raw:
        owner = (r.get("owner") or {}).get("login") or ""
        out.append({
            "name": r.get("name") or "",
            "owner": owner,
            "key": f"github.com/{owner}/{r.get('name') or ''}".lower(),
            "description": r.get("description") or "",
            "private": bool(r.get("isPrivate")),
            "archived": bool(r.get("isArchived")),
            "pushed_at": (r.get("pushedAt") or "")[:10],
            "language": (r.get("primaryLanguage") or {}).get("name") or "",
            "url": r.get("url") or "",
        })
    return out


# ------------------------------------------------------------------ records

def _path_ref(path: str) -> str:
    return path.replace("\\", "/").lower()


def repo_text(s: dict) -> str:
    """The prose the embedder sees — front-load the names/paths recall keys on."""
    lines = [f"# {s['name']} — project state", f"Path: {s['path']}"]
    gh = s.get("github")
    if gh:
        vis = "private" if gh["private"] else "public"
        archived = ", ARCHIVED" if gh["archived"] else ""
        lines.append(f"GitHub: {gh['owner']}/{gh['name']} ({vis}{archived}) — {gh['url']}")
    elif s.get("remote"):
        lines.append(f"Remote: {s['remote']}")
    else:
        lines.append("Remote: none (local-only repo)")
    if s.get("description"):
        lines.append(f"Description: {s['description']}")
    branch = s.get("branch") or "unknown"
    dirty = s.get("dirty_files", 0)
    state = "clean working tree" if not dirty else f"{dirty} uncommitted change(s)"
    lines.append(f"Branch: {branch} ({state})")
    if s.get("commits"):
        lines.append(f"Last commit: {s['commits'][0]}")
        lines.append("Recent commits:")
        lines.extend(f"- {c}" for c in s["commits"])
    if s.get("readme"):
        lines.append("README excerpt:")
        lines.append(s["readme"])
    return "\n".join(lines)


def dir_text(s: dict) -> str:
    lines = [
        f"# {s['name']} — project folder (not a git repo)",
        f"Path: {s['path']}",
    ]
    if s.get("description"):
        lines.append(f"Description: {s['description']}")
    if s.get("entries"):
        lines.append("Contents: " + ", ".join(s["entries"]))
    if s.get("readme"):
        lines.append("README excerpt:")
        lines.append(s["readme"])
    return "\n".join(lines)


def github_only_text(gh: dict) -> str:
    vis = "private" if gh["private"] else "public"
    archived = " (ARCHIVED)" if gh["archived"] else ""
    lines = [
        f"# {gh['name']} — GitHub repo, no local clone found{archived}",
        f"GitHub: {gh['owner']}/{gh['name']} ({vis}) — {gh['url']}",
        f"Last pushed: {gh['pushed_at'] or 'unknown'}   Language: {gh['language'] or 'unknown'}",
    ]
    if gh["description"]:
        lines.append(f"Description: {gh['description']}")
    lines.append("To work on it, clone it first — there is no local checkout on this machine.")
    return "\n".join(lines)


def build_records(summaries: list[dict], gh_repos: list[dict]) -> list[dict]:
    """JSONL records for rag_index: local projects by path-ref, then every
    GitHub repo the scan didn't find a clone of, by github ref."""
    records, seen_keys = [], set()
    for s in summaries:
        text = repo_text(s) if s["kind"] == "repo" else dir_text(s)
        records.append({"source": SOURCE, "ref": _path_ref(s["path"]), "text": text})
        gh = s.get("github")
        if gh:
            seen_keys.add(gh["key"])
    for gh in gh_repos:
        if gh["key"] not in seen_keys:
            records.append({
                "source": SOURCE,
                "ref": f"github:{gh['owner']}/{gh['name']}".lower(),
                "text": github_only_text(gh),
            })
    return records


def render_map(summaries: list[dict], gh_repos: list[dict], now: str) -> str:
    """state/projects-map.md — the human-readable 'where do I go look' file."""
    seen_keys = {s["github"]["key"] for s in summaries if s.get("github")}
    lines = [
        "# Projects map — where everything lives",
        "",
        f"_Generated by `scripts/rag_projects.py` — {now}. Regenerable cache; do not hand-edit._",
        "",
        "## Local projects",
        "",
        "| Project | Path | Branch | Last commit | About |",
        "|---------|------|--------|-------------|-------|",
    ]
    for s in sorted(summaries, key=lambda x: x["name"].lower()):
        if s["kind"] == "repo":
            last = s["commits"][0] if s.get("commits") else "—"
            branch = s.get("branch") or "?"
            if s.get("dirty_files"):
                branch += f" ({s['dirty_files']} dirty)"
        else:
            last, branch = "—", "not a repo"
        about = (s.get("description") or "").replace("|", "/")[:100]
        lines.append(f"| {s['name']} | `{s['path']}` | {branch} | {last[:10]} | {about} |")
    remote_only = [g for g in gh_repos if g["key"] not in seen_keys]
    if remote_only:
        lines += [
            "",
            "## GitHub only — no local clone on this machine",
            "",
            "| Repo | Visibility | Last pushed | Language | About |",
            "|------|-----------|-------------|----------|-------|",
        ]
        for g in sorted(remote_only, key=lambda x: x["pushed_at"], reverse=True):
            vis = "private" if g["private"] else "public"
            if g["archived"]:
                vis += ", archived"
            about = (g["description"] or "").replace("|", "/")[:100]
            lines.append(
                f"| [{g['owner']}/{g['name']}]({g['url']}) | {vis} | {g['pushed_at']} "
                f"| {g['language'] or '—'} | {about} |"
            )
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- prune

def reconcile_index(conn, current_refs: set[str]) -> int:
    """Drop ``project`` docs whose ref vanished from the scan (moved/deleted
    project). Only this source is touched; the salience access ledger is
    measurement, never pruned (same contract as ``rag_index --rebuild``)."""
    stale = [
        row[0]
        for row in conn.execute("SELECT ref FROM docs WHERE source = ?", (SOURCE,))
        if row[0] not in current_refs
    ]
    for ref in stale:
        doc_id = f"{SOURCE}:{ref}"
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    conn.commit()
    return len(stale)


# --------------------------------------------------------------------- main

def scan(cfg: dict, gh_by_key: dict) -> list[dict]:
    exclude = cfg["exclude_names"]
    repos: list[Path] = []
    for root in cfg["roots"]:
        repos.extend(find_repos(Path(root), cfg["max_depth"], exclude))
    for extra in cfg["extras"]:
        p = Path(extra)
        if p.exists() and p not in repos:
            repos.append(p)

    summaries = []
    repo_paths = set()
    for r in repos:
        if str(r) in repo_paths:
            continue
        repo_paths.add(str(r))
        if _is_repo(r):
            summaries.append(summarize_repo(r, gh_by_key))
        else:
            summaries.append(summarize_plain_dir(r))

    # unversioned top-level project dirs (e.g. workspace/3d_prints) — light summaries,
    # skipping any dir that merely *contains* found repos (workspace/repos is a shelf,
    # not a project)
    for root in cfg["non_git_roots"]:
        rootp = Path(root)
        if not rootp.exists():
            continue
        try:
            children = sorted(p for p in rootp.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if child.name in set(exclude) or child.name.startswith("."):
                continue
            if str(child) in repo_paths:
                continue
            prefix = str(child) + os.sep
            if any(p.startswith(prefix) for p in repo_paths):
                continue  # a container of repos, not a project itself
            summaries.append(summarize_plain_dir(child))
    return summaries


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan the owner's projects into the RAG index.")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--out", default=str(JSONL_PATH), help="JSONL output path")
    ap.add_argument("--map", default=str(MAP_PATH), help="projects-map.md output path")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    ap.add_argument("--ingest", action="store_true",
                    help="also embed + upsert into the RAG index (graceful when Ollama is down)")
    ap.add_argument("--no-github", action="store_true", help="skip the gh repo list")
    ap.add_argument("--no-prune", action="store_true",
                    help="don't reconcile vanished projects out of the index")
    ap.add_argument("--dry-run", action="store_true", help="list what would be scanned")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    gh = [] if args.no_github or not cfg["github"]["enabled"] \
        else github_repos(cfg["github"]["limit"])
    gh_by_key = {g["key"]: g for g in gh}

    summaries = scan(cfg, gh_by_key)
    records = build_records(summaries, gh)

    if args.dry_run:
        for s in summaries:
            print(f"{s['kind']:4} {s['path']}")
        print(f"-- {len(summaries)} local projects, {len(gh)} GitHub repos, "
              f"{len(records)} records")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    Path(args.map).write_text(render_map(summaries, gh, now), encoding="utf-8")
    print(f"scanned {len(summaries)} local projects, {len(gh)} GitHub repos "
          f"→ {len(records)} records")
    print(f"wrote {out} + {args.map}")

    if not args.ingest:
        print(f"(not indexed — run: python rag_index.py --ingest {out})")
        return 0

    conn = rc.connect(args.db)
    try:
        added, updated, skipped = ri.index_records(conn, records, rc.load_env())
    except rc.OllamaError as exc:
        print(f"embedder unavailable — map + JSONL written, index not updated: {exc}",
              file=sys.stderr)
        return 3
    pruned = 0
    if not args.no_prune:
        pruned = reconcile_index(conn, {r["ref"] for r in records})
    print(f"indexed: +{added} new, ~{updated} updated, {skipped} unchanged, "
          f"-{pruned} vanished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
