#!/usr/bin/env python3
"""Tests for presence.py's Claude-account identity stamp — the daemon half of the credential guard
(the watchdog half — comparing this stamp against the live login — belongs to the control script's
credential check).

The gap being closed: a code merge deploys itself, a LOGIN did not. The warm ``claude`` CLI session
reads its auth once, at spawn, so ``claude /login`` changed nothing until an unrelated merge happened to
bounce the daemon — a login swap could sit unapplied for as long as it took the next merge to land.

What these pin:

* the identity read is **only** ``oauthAccount``'s three identity fields — a token is never read,
  copied or stamped, and ``~/.claude/.credentials.json`` is not opened at all;
* every unreadable shape (missing, malformed, no ``oauthAccount``, no ``accountUuid``) returns ``None``
  rather than raising, because this runs on the daemon's own startup path — a bug here must never be
  the thing that takes a healthy daemon down;
* ``write_lock`` stamps the identity into ``presence.lock``, and ``beat_lock`` **restores it** into a
  lock it had to rebuild from a failed read. Without that restore a single mid-write collision would
  silently un-stamp a running daemon and the watchdog would read "nothing to compare" forever — the
  guard would look present while doing nothing, exactly the silently-dropped-counter shape
  the daemon's revive design warns about;
* ``beat_lock`` does **not** re-read the config, so the stamp keeps meaning "what the daemon started
  with" instead of quietly becoming "what is on disk right now" (which would answer its own question
  and the guard would never fire).

Stdlib ``unittest`` only; each test uses a throwaway temp dir so nothing touches the real ``state/``.

Run:  python -m unittest seneschal.scripts.test_presence_claude_identity  (or)  python test_presence_claude_identity.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _config(account="acct-1", org="org-1", email="owner@example.invalid", **extra):
    acct = {"accountUuid": account, "organizationUuid": org, "emailAddress": email}
    acct.update(extra)
    return {"numStartups": 3, "oauthAccount": acct}


class ReadClaudeIdentity(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claude-identity-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cfg = os.path.join(self.dir, ".claude.json")

    def test_reads_the_three_identity_fields(self):
        _write_json(self.cfg, _config())
        self.assertEqual(
            pr.read_claude_identity(self.cfg),
            {"account_uuid": "acct-1", "organization_uuid": "org-1", "email": "owner@example.invalid"},
        )

    def test_reads_nothing_else_from_the_config(self):
        """No token, no key, no project history — three identity fields and nothing more.

        The whole file is in scope of the open(), so the guarantee that matters is what LEAVES the
        function: a fixed three-key dict. A future edit that widened it would fail here.
        """
        cfg = _config(
            organizationType="claude_max",
            organizationRateLimitTier="default_max_20x",
            accessToken="sk-ant-oat-DO-NOT-STAMP-THIS",
        )
        cfg["oauthToken"] = "sk-ant-DO-NOT-STAMP-THIS-EITHER"
        _write_json(self.cfg, cfg)
        got = pr.read_claude_identity(self.cfg)
        self.assertEqual(set(got), {"account_uuid", "organization_uuid", "email"})
        self.assertNotIn("DO-NOT-STAMP-THIS", json.dumps(got))

    def test_missing_file_is_none_not_an_exception(self):
        self.assertIsNone(pr.read_claude_identity(os.path.join(self.dir, "nope.json")))

    def test_malformed_json_is_none_not_an_exception(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("{ half-written")
        self.assertIsNone(pr.read_claude_identity(self.cfg))

    def test_no_oauth_account_is_none(self):
        _write_json(self.cfg, {"numStartups": 3})
        self.assertIsNone(pr.read_claude_identity(self.cfg))

    def test_oauth_account_of_the_wrong_type_is_none(self):
        _write_json(self.cfg, {"oauthAccount": "not-an-object"})
        self.assertIsNone(pr.read_claude_identity(self.cfg))

    def test_no_account_uuid_is_none(self):
        """An oauthAccount with no accountUuid cannot key anything, so it is 'I could not tell'."""
        _write_json(self.cfg, {"oauthAccount": {"emailAddress": "owner@example.invalid"}})
        self.assertIsNone(pr.read_claude_identity(self.cfg))

    def test_missing_org_and_email_degrade_to_empty_strings(self):
        _write_json(self.cfg, {"oauthAccount": {"accountUuid": "acct-1"}})
        self.assertEqual(
            pr.read_claude_identity(self.cfg),
            {"account_uuid": "acct-1", "organization_uuid": "", "email": ""},
        )


class ClaudeConfigPath(unittest.TestCase):
    """The path resolution must match seneschald-control.ps1's $ClaudeConfigFile step for step: the guard
    compares what one wrote against what the other read, so two different files would make the
    comparison nonsense rather than merely wrong."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claude-cfgpath-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_defaults_to_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(
                pr.claude_config_path(),
                os.path.join(os.path.expanduser("~"), ".claude.json"),
            )

    def test_honours_claude_config_dir_when_it_holds_the_file(self):
        cfg = os.path.join(self.dir, ".claude.json")
        _write_json(cfg, _config())
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}):
            self.assertEqual(pr.claude_config_path(), cfg)

    def test_falls_back_to_home_when_the_config_dir_has_no_file(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}):
            self.assertEqual(
                pr.claude_config_path(),
                os.path.join(os.path.expanduser("~"), ".claude.json"),
            )


class LockStamping(unittest.TestCase):
    """write_lock stamps it; beat_lock must never lose it."""

    IDENT = {"account_uuid": "acct-1", "organization_uuid": "org-1", "email": "owner@example.invalid"}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claude-lock-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # write_lock also records a boot attempt; keep the module global from leaking between tests.
        self.addCleanup(setattr, pr, "_BOOT_CLAUDE_IDENTITY", None)

    def test_write_lock_stamps_the_boot_identity(self):
        with mock.patch.object(pr, "read_claude_identity", return_value=dict(self.IDENT)):
            pr.write_lock(self.dir)
        lock = _read_json(pr.lock_path(self.dir))
        self.assertEqual(lock["claude_identity"], self.IDENT)
        self.assertIn("pid", lock)
        self.assertIn("started_at", lock)

    def test_write_lock_omits_the_stamp_when_the_identity_is_unreadable(self):
        """Absent means 'nothing to compare' downstream — never 'the account changed'."""
        with mock.patch.object(pr, "read_claude_identity", return_value=None):
            pr.write_lock(self.dir)
        self.assertNotIn("claude_identity", _read_json(pr.lock_path(self.dir)))

    def test_beat_lock_keeps_the_stamp(self):
        with mock.patch.object(pr, "read_claude_identity", return_value=dict(self.IDENT)):
            pr.write_lock(self.dir)
        pr.beat_lock(self.dir)
        self.assertEqual(_read_json(pr.lock_path(self.dir))["claude_identity"], self.IDENT)

    def test_beat_lock_restores_the_stamp_after_an_unreadable_lock(self):
        """load_json falls back to {} on a mid-write collision (a real failure mode on
        Windows — see sentinel.save_json). A heartbeat that rebuilt the lock without the identity would silently
        un-stamp a running daemon and leave the guard inert until the next boot."""
        with mock.patch.object(pr, "read_claude_identity", return_value=dict(self.IDENT)):
            pr.write_lock(self.dir)
        with open(pr.lock_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ mid-write garbage")
        pr.beat_lock(self.dir)
        self.assertEqual(_read_json(pr.lock_path(self.dir))["claude_identity"], self.IDENT)

    def test_beat_lock_does_not_re_read_the_config(self):
        """The stamp means 'what the daemon started with'. If a heartbeat re-read the config it would
        quietly become 'what is on disk right now' — the guard would answer its own question and could
        never fire."""
        with mock.patch.object(pr, "read_claude_identity", return_value=dict(self.IDENT)):
            pr.write_lock(self.dir)
        with mock.patch.object(pr, "read_claude_identity", side_effect=AssertionError("re-read!")) as spy:
            pr.beat_lock(self.dir)
        spy.assert_not_called()
        self.assertEqual(_read_json(pr.lock_path(self.dir))["claude_identity"], self.IDENT)

    def test_beat_lock_on_a_pre_guard_lock_adds_nothing(self):
        """A daemon whose boot found no identity must not acquire one from a later heartbeat."""
        pr._BOOT_CLAUDE_IDENTITY = None
        with mock.patch.object(pr, "read_claude_identity", return_value=None):
            pr.write_lock(self.dir)
        pr.beat_lock(self.dir)
        self.assertNotIn("claude_identity", _read_json(pr.lock_path(self.dir)))


if __name__ == "__main__":
    unittest.main()
