#!/usr/bin/env python3
"""Tests for the project-state RAG layer (``rag_projects.py``).

Stdlib ``unittest`` only — no Ollama (``embed_texts`` is monkeypatched where
indexing needs it, as in ``test_salience_access.py``) and no ``gh`` (the runner
is injectable). Real ``git`` IS used for the repo-summary tests (CI has it);
those skip gracefully if git is somehow absent.

Run:  python -m unittest seneschal.scripts.test_rag_projects   (or)   python test_rag_projects.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rag_common as rc   # noqa: E402
import rag_index as ri    # noqa: E402
import rag_projects as rp  # noqa: E402

GIT = shutil.which("git") is not None


def _make_repo(path: Path, *, remote: str | None = None, commits: int = 1) -> None:
    """git init + N commits, isolated from user/global config."""
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def run(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=env,
                       capture_output=True)

    run("init", "-q", "-b", "main")
    for i in range(commits):
        (path / "file.txt").write_text(f"rev {i}\n", encoding="utf-8")
        run("add", "file.txt")
        run("commit", "-q", "-m", f"commit {i}")
    if remote:
        run("remote", "add", "origin", remote)


class NormalizeRemoteUnit(unittest.TestCase):
    def test_equivalent_forms_share_one_key(self):
        forms = [
            "https://github.com/Kumouri/lavalamp.git",
            "https://github.com/kumouri/lavalamp/",
            "git@github.com:kumouri/lavalamp.git",
            "ssh://git@github.com/kumouri/lavalamp",
        ]
        keys = {rp.normalize_remote(f) for f in forms}
        self.assertEqual(keys, {"github.com/kumouri/lavalamp"})

    def test_empty_and_none_are_none(self):
        self.assertIsNone(rp.normalize_remote(""))
        self.assertIsNone(rp.normalize_remote("   "))


class ConfigUnit(unittest.TestCase):
    def test_defaults_fill_in(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text('{"roots": ["/tmp/x"]}', encoding="utf-8")
            cfg = rp.load_config(p)
            self.assertEqual(cfg["roots"], ["/tmp/x"])
            self.assertEqual(cfg["max_depth"], 3)
            self.assertTrue(cfg["github"]["enabled"])
            self.assertIn("node_modules", cfg["exclude_names"])

    def test_missing_config_exits_with_pointer(self):
        with self.assertRaises(SystemExit) as ctx:
            rp.load_config(Path(tempfile.gettempdir()) / "no-such-config-xyz.json")
        self.assertIn("project-roots.example.json", str(ctx.exception))


class FindReposUnit(unittest.TestCase):
    def test_prunes_below_repos_and_excluded_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "shelf" / "repo-a" / ".git").mkdir(parents=True)
            (root / "shelf" / "repo-a" / "nested" / ".git").mkdir(parents=True)
            (root / "node_modules" / "repo-b" / ".git").mkdir(parents=True)
            (root / ".hidden" / "repo-c" / ".git").mkdir(parents=True)
            (root / "plain").mkdir()
            found = rp.find_repos(root, 3, rp.DEFAULT_EXCLUDES)
            names = {p.name for p in found}
            self.assertEqual(names, {"repo-a"})  # nested, excluded, hidden all pruned

    def test_gitfile_worktree_counts_as_repo(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"
            wt.mkdir()
            (wt / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            self.assertEqual(rp.find_repos(Path(d), 2, set()), [wt])

    def test_depth_cap(self):
        with tempfile.TemporaryDirectory() as d:
            deep = Path(d) / "a" / "b" / "c" / "repo"
            (deep / ".git").mkdir(parents=True)
            self.assertEqual(rp.find_repos(Path(d), 2, set()), [])
            self.assertEqual([p.name for p in rp.find_repos(Path(d), 4, set())], ["repo"])


@unittest.skipUnless(GIT, "git not available")
class SummarizeRepoUnit(unittest.TestCase):
    def test_summary_carries_branch_commits_readme_and_github_match(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "proj"
            _make_repo(repo, remote="git@github.com:kumouri/proj.git", commits=2)
            (repo / "README.md").write_text("# Proj\nDoes a thing.", encoding="utf-8")
            gh = {"key": "github.com/kumouri/proj", "name": "proj", "owner": "kumouri",
                  "description": "gh desc", "private": True, "archived": False,
                  "pushed_at": "2026-07-01", "language": "Python", "url": "u"}
            s = rp.summarize_repo(repo, {gh["key"]: gh})
            self.assertEqual(s["branch"], "main")
            self.assertEqual(len(s["commits"]), 2)
            self.assertIn("commit 1", s["commits"][0])  # newest first
            self.assertEqual(s["github"], gh)
            self.assertEqual(s["description"], "gh desc")
            self.assertEqual(s["dirty_files"], 1)  # untracked README counts
            text = rp.repo_text(s)
            self.assertIn("proj — project state", text)
            self.assertIn("kumouri/proj", text)
            self.assertIn("Does a thing.", text)

    def test_local_only_repo_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "solo"
            _make_repo(repo)
            s = rp.summarize_repo(repo, {})
            self.assertIsNone(s["remote"])
            self.assertIn("local-only", rp.repo_text(s))


class PlainDirUnit(unittest.TestCase):
    def test_dir_summary_lists_contents(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "3d_prints"
            (p / "widget").mkdir(parents=True)
            (p / "notes.txt").write_text("x", encoding="utf-8")
            s = rp.summarize_plain_dir(p)
            self.assertEqual(s["kind"], "dir")
            self.assertIn("widget", s["entries"])
            self.assertIn("not a git repo", rp.dir_text(s))


class GithubUnit(unittest.TestCase):
    def test_parses_gh_json(self):
        payload = json.dumps([{
            "name": "lavalamp", "owner": {"login": "kumouri"},
            "description": "IdleOn bot", "isPrivate": True, "isArchived": False,
            "pushedAt": "2026-07-01T14:15:08Z",
            "primaryLanguage": {"name": "JavaScript"}, "url": "https://x",
        }])

        def fake_runner(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout=payload, stderr="")

        repos = rp.github_repos(runner=fake_runner)
        self.assertEqual(repos[0]["key"], "github.com/kumouri/lavalamp")
        self.assertEqual(repos[0]["pushed_at"], "2026-07-01")

    def test_gh_missing_is_empty(self):
        def boom(*a, **k):
            raise OSError("no gh")
        self.assertEqual(rp.github_repos(runner=boom), [])


class RecordsUnit(unittest.TestCase):
    def _summary(self, name="proj", gh=None):
        return {"kind": "repo", "name": name, "path": f"C:\\w\\{name}", "branch": "main",
                "dirty_files": 0, "remote": None, "github": gh,
                "commits": ["2026-07-01 hi"], "readme": "", "description": ""}

    def test_local_plus_unclonned_github(self):
        gh_local = {"key": "github.com/kumouri/proj", "name": "proj", "owner": "kumouri",
                    "description": "", "private": True, "archived": False,
                    "pushed_at": "2026-07-01", "language": "", "url": "u"}
        gh_remote = dict(gh_local, key="github.com/kumouri/other", name="other")
        records = rp.build_records([self._summary(gh=gh_local)], [gh_local, gh_remote])
        refs = {r["ref"] for r in records}
        self.assertEqual(refs, {"c:/w/proj", "github:kumouri/other"})
        self.assertTrue(all(r["source"] == "project" for r in records))
        remote_rec = next(r for r in records if r["ref"].startswith("github:"))
        self.assertIn("no local clone", remote_rec["text"])

    def test_map_renders_both_sections(self):
        gh_remote = {"key": "github.com/kumouri/other", "name": "other", "owner": "kumouri",
                     "description": "d", "private": False, "archived": False,
                     "pushed_at": "2026-07-01", "language": "Go", "url": "https://x"}
        md = rp.render_map([self._summary()], [gh_remote], "2026-07-15T00:00:00+00:00")
        self.assertIn("## Local projects", md)
        self.assertIn("| proj |", md)
        self.assertIn("## GitHub only", md)
        self.assertIn("kumouri/other", md)


class ReconcileUnit(unittest.TestCase):
    def test_vanished_project_docs_pruned_other_sources_kept(self):
        fake_vec = [1.0, 0.0]
        real_embed = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, timeout=0: [fake_vec for _ in texts]
        try:
            with tempfile.TemporaryDirectory() as d:
                conn = rc.connect(os.path.join(d, "rag.sqlite"))
                try:
                    ri.index_records(conn, [
                        {"source": "project", "ref": "c:/w/gone", "text": "old project"},
                        {"source": "project", "ref": "c:/w/kept", "text": "live project"},
                        {"source": "journal", "ref": "2026-07-01", "text": "a journal day"},
                    ], dict(rc.DEFAULTS))
                    pruned = rp.reconcile_index(conn, {"c:/w/kept"})
                    self.assertEqual(pruned, 1)
                    left = {r[0] for r in conn.execute("SELECT id FROM docs")}
                    self.assertEqual(left, {"project:c:/w/kept", "journal:2026-07-01"})
                    orphans = conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE doc_id = 'project:c:/w/gone'"
                    ).fetchone()[0]
                    self.assertEqual(orphans, 0)
                finally:
                    conn.close()  # Windows: an open handle blocks tempdir cleanup
        finally:
            rc.embed_texts = real_embed


if __name__ == "__main__":
    unittest.main()
