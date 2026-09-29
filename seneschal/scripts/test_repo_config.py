"""Tests for repo_config.py — the PR guards' repository/base-branch config (file, then git, never raises)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402


def fake_git(answers: dict):
    """A runner answering `git …` by the first argv word after the `-c` pair; unknown -> rc 1."""
    def run(argv, cwd):
        key = " ".join(argv[3:])
        for k, v in answers.items():
            if key.startswith(k):
                return (0, v) if v is not None else (1, "")
        return 1, ""
    return run


class ParseRemoteTests(unittest.TestCase):
    def test_https_scp_and_ssh_forms(self):
        self.assertEqual(rc.parse_remote("https://github.com/example/repo.git"), "example/repo")
        self.assertEqual(rc.parse_remote("https://github.com/example/repo"), "example/repo")
        self.assertEqual(rc.parse_remote("git@github.com:example/repo.git"), "example/repo")
        self.assertEqual(rc.parse_remote("ssh://git@github.com/example/repo.git"), "example/repo")

    def test_garbage_is_empty(self):
        self.assertEqual(rc.parse_remote(""), "")
        self.assertEqual(rc.parse_remote("not a url"), "")
        self.assertEqual(rc.parse_remote(None), "")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _write(self, name, text):
        with open(os.path.join(self.d, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_example_is_read_when_owner_file_absent(self):
        self._write(rc.REFERENCES_EXAMPLE_FILE, json.dumps({"base_branch": "trunk"}))
        self.assertEqual(rc.load(self.d)["base_branch"], "trunk")

    def test_owner_file_wins_and_a_corrupt_one_reads_empty_not_the_example(self):
        self._write(rc.REFERENCES_EXAMPLE_FILE, json.dumps({"base_branch": "trunk"}))
        self._write(rc.REFERENCES_FILE, "{not json")
        self.assertEqual(rc.load(self.d), {})

    def test_neither_file_is_empty(self):
        self.assertEqual(rc.load(self.d), {})

    def test_shipped_example_parses_and_sets_nothing(self):
        cfg = rc.load(rc.REFERENCES_DIR) if not os.path.exists(
            os.path.join(rc.REFERENCES_DIR, rc.REFERENCES_FILE)) else None
        if cfg is None:
            self.skipTest("an owner pr-guard.json is installed")
        self.assertEqual(cfg.get("watched_repos"), [])
        self.assertIsNone(cfg.get("base_branch"))


class ResolutionTests(unittest.TestCase):
    ORIGIN = {"remote get-url origin": "https://github.com/example/repo.git"}

    def test_watched_repos_config_wins_and_invalid_slugs_drop(self):
        cfg = {"watched_repos": ["example/one", "nope", " example/two "]}
        self.assertEqual(rc.watched_repos(cfg, runner=fake_git(self.ORIGIN)),
                         ("example/one", "example/two"))

    def test_watched_repos_falls_back_to_origin_then_empty(self):
        self.assertEqual(rc.watched_repos({}, runner=fake_git(self.ORIGIN)), ("example/repo",))
        self.assertEqual(rc.watched_repos({}, runner=fake_git({})), ())

    def test_base_branch_ladder(self):
        self.assertEqual(rc.base_branch({"base_branch": "trunk"}, runner=fake_git({})), "trunk")
        self.assertEqual(rc.base_branch({}, runner=fake_git({"rev-parse": "abc123"})), "develop")
        self.assertEqual(rc.base_branch({}, runner=fake_git({"symbolic-ref": "origin/master"})), "master")
        self.assertEqual(rc.base_branch({}, runner=fake_git({})), "main")

    def test_protected_branches_config_adds_never_removes(self):
        got = rc.protected_branches({"protected_branches": ["release", ""]})
        self.assertTrue(rc.PROTECTED_FLOOR <= got)
        self.assertIn("release", got)
        self.assertEqual(rc.protected_branches({}), rc.PROTECTED_FLOOR)

    def test_deploy_on_merge_config_else_origin_on_main(self):
        self.assertEqual(rc.deploy_on_merge({"deploy_on_merge": {"example/x": "develop"}},
                                            runner=fake_git(self.ORIGIN)), {"example/x": "develop"})
        self.assertEqual(rc.deploy_on_merge({}, runner=fake_git(self.ORIGIN)),
                         {"example/repo": rc.DEFAULT_DEPLOY_BRANCH})
        self.assertEqual(rc.deploy_on_merge({}, runner=fake_git({})), {})

    def test_a_raising_runner_never_escapes(self):
        def boom(argv, cwd):
            raise OSError("no git")
        self.assertEqual(rc.origin_repo(runner=boom), "")
        self.assertEqual(rc.base_branch({}, runner=boom), "main")


if __name__ == "__main__":
    unittest.main()
