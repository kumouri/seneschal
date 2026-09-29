"""Tests for promote_intel's merge invariants and its Dream-step stamp.

The merge is a pure function precisely so these can run with no git, no network and no clock. The
invariant that matters most: an archon may never weaken one of the OWNER's decisions. A charter can
ask for that in prose; this is where it is enforced.

Fixtures are neutral (Acme / Globex / Initech); `updated` values are fixed strings, never "today".
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import promote_intel as pi  # noqa: E402


def _owner_entry(adjust=-18, tags=("layoffs",)):
    return {"adjust": adjust, "tags": list(tags), "note": "the owner's call",
            "source": "owner: seeded from profile.json", "updated": "2026-07-18"}


def _archon_entry(company="Initech", adjust=-5, tags=("layoffs",), note="from a work-up"):
    return {"company": company, "adjust": adjust, "tags": list(tags), "note": note,
            "source": "Proteus work-up initech-swe", "updated": "2026-07-18",
            "recorded_at": "2026-07-18T12:00:00Z"}


class MergeTests(unittest.TestCase):
    def test_adds_a_new_company(self):
        ledger, notes, refused = pi.merge_intel({"companies": {}}, [_archon_entry()])
        self.assertIn("Initech", ledger["companies"])
        self.assertEqual(ledger["companies"]["Initech"]["adjust"], -5)
        self.assertEqual(refused, [])

    def test_updates_in_place_rather_than_duplicating(self):
        tracked = {"companies": {"Initech": {"adjust": -3, "tags": [], "note": "", "source": "p",
                                             "updated": "2026-07-01"}}}
        ledger, _, _ = pi.merge_intel(tracked, [_archon_entry(adjust=-9)])
        self.assertEqual(len(ledger["companies"]), 1)
        self.assertEqual(ledger["companies"]["Initech"]["adjust"], -9)

    def test_case_insensitive_match_keeps_the_existing_spelling(self):
        tracked = {"companies": {"Acme Corp": {"adjust": -2, "tags": [], "note": "", "source": "p",
                                               "updated": "2026-07-01"}}}
        ledger, _, _ = pi.merge_intel(tracked, [_archon_entry(company="acme corp", adjust=-4)])
        self.assertIn("Acme Corp", ledger["companies"])
        self.assertNotIn("acme corp", ledger["companies"])
        self.assertEqual(len(ledger["companies"]), 1)

    def test_clamps_to_the_ceiling(self):
        ledger, notes, _ = pi.merge_intel({"companies": {}}, [_archon_entry(adjust=-999)])
        self.assertEqual(ledger["companies"]["Initech"]["adjust"], -20.0)
        self.assertTrue(any("clamped" in n for n in notes))

    def test_entry_without_a_company_is_refused_not_crashed(self):
        bad = {"adjust": -5, "source": "x", "updated": "2026-07-18"}
        ledger, _, refused = pi.merge_intel({"companies": {}}, [bad])
        self.assertEqual(refused, [bad])
        self.assertEqual(ledger["companies"], {})

    def test_does_not_mutate_the_caller(self):
        tracked = {"companies": {}}
        pi.merge_intel(tracked, [_archon_entry()])
        self.assertEqual(tracked, {"companies": {}})

    # --- the rule that matters: the owner's entries ----------------------------------------------

    def test_refuses_to_move_the_owners_adjustment_toward_zero(self):
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18)}}
        entry = _archon_entry(company="Globex", adjust=-3)
        ledger, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [entry])
        self.assertEqual(ledger["companies"]["Globex"]["adjust"], -18)
        self.assertTrue(any("REFUSED" in n and "toward zero" in n for n in notes))

    def test_refuses_to_flip_the_sign(self):
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18)}}
        entry = _archon_entry(company="Globex", adjust=19)
        _, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [entry])
        self.assertTrue(any("flips the sign" in n for n in notes))

    def test_refuses_to_drop_the_owners_tags(self):
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18, tags=("layoffs", "rto"))}}
        entry = _archon_entry(company="Globex", adjust=-20, tags=("layoffs",))
        _, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [entry])
        self.assertTrue(any("drops the owner's tag" in n for n in notes))

    def test_allows_strengthening_an_owner_entry(self):
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18)}}
        entry = _archon_entry(company="Globex", adjust=-20, note="the owner's call")
        ledger, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [])
        self.assertEqual(ledger["companies"]["Globex"]["adjust"], -20)
        self.assertTrue(any("strengthened" in n for n in notes))

    def test_refuses_to_replace_the_owners_note_even_while_strengthening(self):
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18)}}
        entry = _archon_entry(company="Globex", adjust=-20, note="different words")
        ledger, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [entry])
        self.assertEqual(ledger["companies"]["Globex"]["note"], "the owner's call")
        self.assertTrue(any("REFUSED" in n and "replaces the owner's note" in n for n in notes))

    def test_refuses_to_replace_the_note_on_a_zero_adjust_reading_rule(self):
        # A reading-rule entry carries all its meaning in `note`, with `adjust: 0.0`; the magnitude
        # test can never fire against a zero baseline, so only the note check protects it.
        rule = _owner_entry(adjust=0.0, tags=("policy",))
        rule["note"] = "never apply to a company that requires relocation"
        tracked = {"companies": {"Globex": rule}}
        entry = _archon_entry(company="Globex", adjust=5, tags=("policy",), note="looks fine now")
        ledger, notes, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [entry])
        self.assertEqual(ledger["companies"]["Globex"]["note"],
                         "never apply to a company that requires relocation")
        self.assertEqual(ledger["companies"]["Globex"]["adjust"], 0.0)

    def test_allows_reconfirming_a_zero_adjust_reading_rule_with_the_same_note(self):
        rule = _owner_entry(adjust=0.0, tags=("policy",))
        rule["note"] = "never apply to a company that requires relocation"
        tracked = {"companies": {"Globex": rule}}
        entry = _archon_entry(company="Globex", adjust=0, tags=("policy",),
                              note="never apply to a company that requires relocation")
        _, _, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [])

    def test_archon_entries_are_freely_overwritable(self):
        tracked = {"companies": {"Acme": {"adjust": -15, "tags": ["layoffs"], "note": "",
                                          "source": "Proteus work-up acme-1",
                                          "updated": "2026-07-01"}}}
        entry = _archon_entry(company="Acme", adjust=-1, tags=[])
        ledger, _, refused = pi.merge_intel(tracked, [entry])
        self.assertEqual(refused, [])
        self.assertEqual(ledger["companies"]["Acme"]["adjust"], -1)

    def test_owner_marking_is_case_insensitive_and_has_an_explicit_form(self):
        for existing in ({"adjust": -18, "tags": [], "note": "", "source": "OWNER said so"},
                         {"adjust": -18, "tags": [], "note": "", "source": "x", "by_owner": True}):
            with self.subTest(existing=existing):
                entry = _archon_entry(company="Globex", adjust=-2)
                _, _, refused = pi.merge_intel({"companies": {"Globex": existing}}, [entry])
                self.assertEqual(refused, [entry])


class SerializationTests(unittest.TestCase):
    def test_preserves_comment_and_two_space_indent(self):
        text = pi.dump_ledger({"_comment": "keep me", "companies": {"A": {"adjust": -1}}})
        self.assertIn('"_comment": "keep me"', text)
        self.assertIn('\n  "companies"', text)
        self.assertTrue(text.endswith("\n"))

    def test_round_trips(self):
        ledger = {"_comment": "c", "companies": {"A": {"adjust": -1, "tags": ["x"]}}}
        self.assertEqual(json.loads(pi.dump_ledger(ledger)), ledger)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "pending.jsonl"

    def test_skips_a_malformed_line_without_stranding_the_rest(self):
        self.path.write_text(json.dumps(_archon_entry()) + "\n{ not json\n" + json.dumps(
            _archon_entry(company="Acme")) + "\n", encoding="utf-8")
        self.assertEqual([e["company"] for e in pi.load_pending(self.path)], ["Initech", "Acme"])

    def test_missing_queue_is_empty_not_an_error(self):
        self.assertEqual(pi.load_pending(Path(self.dir) / "nope.jsonl"), [])

    def test_prune_landed_drops_what_reached_the_ledger_and_keeps_the_rest(self):
        landed = _archon_entry(company="Initech", adjust=-5)
        not_yet = _archon_entry(company="Acme", adjust=-7)
        self.path.write_text(json.dumps(landed) + "\n" + json.dumps(not_yet) + "\n", encoding="utf-8")
        tracked = {"companies": {"Initech": {"adjust": -5, "tags": ["layoffs"], "note": "from a work-up",
                                             "source": "Proteus work-up initech-swe", "updated": "x"}}}
        self.assertEqual(pi.prune_landed(self.path, tracked), 1)
        self.assertEqual([e["company"] for e in pi.load_pending(self.path)], ["Acme"])

    def test_prune_landed_can_drain_the_queue_to_empty(self):
        landed = _archon_entry(company="Initech", adjust=-5)
        self.path.write_text(json.dumps(landed) + "\n", encoding="utf-8")
        tracked = {"companies": {"Initech": {"adjust": -5, "source": "Proteus work-up initech-swe"}}}
        self.assertEqual(pi.prune_landed(self.path, tracked), 1)
        self.assertEqual(pi.load_pending(self.path), [])

    def test_prune_landed_keeps_a_refused_weakening_entry(self):
        weakening = _archon_entry(company="Globex", adjust=-2)
        self.path.write_text(json.dumps(weakening) + "\n", encoding="utf-8")
        tracked = {"companies": {"Globex": _owner_entry(adjust=-18)}}
        self.assertEqual(pi.prune_landed(self.path, tracked), 0)
        self.assertEqual(len(pi.load_pending(self.path)), 1)

    def test_prune_landed_compares_against_the_clamped_value(self):
        big = _archon_entry(company="Hooli", adjust=-999)
        self.path.write_text(json.dumps(big) + "\n", encoding="utf-8")
        tracked = {"companies": {"Hooli": {"adjust": -20.0, "tags": ["layoffs"], "note": "from a work-up",
                                           "source": "Proteus work-up initech-swe", "updated": "x"}}}
        self.assertEqual(pi.prune_landed(self.path, tracked), 1)

    def test_dedupe_keeps_the_last_proposal_per_company(self):
        deduped = pi.dedupe_pending([_archon_entry(company="Acme", adjust=-5),
                                     _archon_entry(company="Acme", adjust=-9)])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["adjust"], -9)

    def test_corrupt_ledger_raises_rather_than_silently_dropping_companies(self):
        p = Path(self.dir) / "tracked.json"
        p.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            pi.load_tracked(p)

    def test_missing_ledger_is_a_fresh_one(self):
        self.assertEqual(pi.load_tracked(Path(self.dir) / "nope.json"), {"companies": {}})


class LocalPromotionIsTheDefault(unittest.TestCase):
    """`--apply` merges into the local, gitignored ledger and NEVER runs git, gh or a PR."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pending = Path(self.dir) / "pending.jsonl"
        self.tracked = Path(self.dir) / "company-intel.json"
        self.state = Path(self.dir) / "state"

    def _run(self, *extra):
        args = ["--apply", "--pending", str(self.pending), "--tracked", str(self.tracked),
                "--state-dir", str(self.state), *extra]
        with contextlib.redirect_stdout(io.StringIO()):
            return pi.main(args)

    def test_the_default_path_never_runs_git(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")

        def forbidden(*a, **k):
            raise AssertionError(f"the default promotion must not run a subprocess: {a!r}")

        with unittest.mock.patch.object(pi.subprocess, "run", side_effect=forbidden) as run, \
                unittest.mock.patch.object(pi.Runner, "run", side_effect=forbidden) as runner, \
                unittest.mock.patch.object(pi, "promote", side_effect=forbidden) as promote:
            self.assertEqual(self._run(), 0)
        run.assert_not_called()
        runner.assert_not_called()
        promote.assert_not_called()

    def test_it_writes_the_local_ledger_and_prunes_what_landed(self):
        self.tracked.write_text(json.dumps({"_comment": "keep", "companies": {
            "Globex": _owner_entry(adjust=-18)}}), encoding="utf-8")
        weakening = _archon_entry(company="Globex", adjust=-2)
        self.pending.write_text(json.dumps(_archon_entry()) + "\n" + json.dumps(weakening) + "\n",
                                encoding="utf-8")
        self.assertEqual(self._run(), 0)
        ledger = json.loads(self.tracked.read_text(encoding="utf-8"))
        self.assertEqual(ledger["_comment"], "keep")
        self.assertEqual(ledger["companies"]["Initech"]["adjust"], -5)
        self.assertEqual(ledger["companies"]["Globex"]["adjust"], -18, "the owner's entry stands")
        # The landed proposal is pruned; the refused one stays queued for the owner.
        self.assertEqual([e["company"] for e in pi.load_pending(self.pending)], ["Globex"])

    def test_the_ledger_is_gitignored_in_the_archon(self):
        ignore = (Path(pi.__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("company-intel.json", ignore.splitlines())


class PromoteCommandSequence(unittest.TestCase):
    """`promote` through the Runner seam — never the live checkout, only the one file, the base."""

    def test_the_sequence_targets_the_base_and_adds_only_the_ledger(self):
        calls = []

        class FakeRunner:
            def run(self, args, cwd=None, check=True):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="https://example/pr/1\n", stderr="")

        url = pi.promote(FakeRunner(), {"companies": {}}, ["- x"], 1, log=lambda *_: None,
                         base="develop")
        self.assertEqual(url, "https://example/pr/1")
        flat = [" ".join(c) for c in calls]
        self.assertTrue(any("fetch origin develop" in c for c in flat))
        self.assertTrue(any("worktree add" in c and "origin/develop" in c for c in flat))
        adds = [c for c in calls if "add" in c and "worktree" not in c]
        self.assertEqual(len(adds), 1)
        self.assertIn("-f", adds[0], "the ledger is gitignored, so the opt-in PR path must force-add")
        self.assertTrue(adds[0][-1].endswith("company-intel.json"))
        self.assertFalse(any("-A" in c for c in calls), "never `git add -A`")
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] and "develop" in c for c in calls))
        self.assertTrue(any("worktree" in c and "remove" in c for c in calls))


class DreamStepStampTests(unittest.TestCase):
    """2e's ledger row: the promotion stamps itself, and only when it actually ran.

    `main` is driven end-to-end with `--pending` / `--tracked` / `--state-dir` and the real
    `dream_steps`, so a stamp written to the wrong state dir fails here rather than passing and
    going silently missing on the host.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = Path(self.dir) / "state"
        self.pending = Path(self.dir) / "pending.jsonl"
        self.tracked = Path(self.dir) / "tracked.json"
        self.tracked.write_text(json.dumps({"companies": {}}), encoding="utf-8")

    def _ledger(self) -> dict:
        path = self.state / "dream-steps.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _args(self, *extra):
        return ["--pending", str(self.pending), "--tracked", str(self.tracked),
                "--state-dir", str(self.state), *extra]

    def _quiet_main(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return pi.main(args)

    def _stub_promote(self, fn):
        real = pi.promote
        pi.promote = fn
        self.addCleanup(lambda: setattr(pi, "promote", real))

    def test_a_successful_pr_promotion_stamps_2e(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")
        calls = []
        self._stub_promote(lambda *a, **k: calls.append(a) or "https://example/pr/1")
        self.assertEqual(self._quiet_main(self._args("--apply", "--via-pr")), 0)
        self.assertEqual(len(calls), 1, "sanity: the promotion actually ran")
        self.assertIn("last_ok", self._ledger().get("2e", {}))

    def test_a_successful_local_promotion_stamps_2e(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")
        self.assertEqual(self._quiet_main(self._args("--apply")), 0)
        self.assertIn("last_ok", self._ledger().get("2e", {}))

    def test_via_pr_without_apply_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pi.main(self._args("--dry-run", "--via-pr"))

    def test_an_empty_queue_still_stamps_2e(self):
        self.assertEqual(self._quiet_main(self._args("--apply")), 0)
        self.assertIn("last_ok", self._ledger().get("2e", {}))

    def test_a_dry_run_does_not_stamp(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")
        self.assertEqual(self._quiet_main(self._args("--dry-run")), 0)
        self.assertIsNone(self._ledger().get("2e", {}).get("last_ok"))

    def test_a_failed_promotion_does_not_stamp(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, ["git", "push"])

        self._stub_promote(boom)
        with self.assertRaises(subprocess.CalledProcessError):
            self._quiet_main(self._args("--apply", "--via-pr"))
        self.assertIsNone(self._ledger().get("2e", {}).get("last_ok"))

    def test_an_unreadable_ledger_does_not_stamp(self):
        self.tracked.write_text("{ not json", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._quiet_main(self._args("--apply")), 1)
        self.assertIsNone(self._ledger().get("2e", {}).get("last_ok"))

    def test_a_raising_dream_steps_changes_neither_exit_code_nor_output(self):
        self.pending.write_text(json.dumps(_archon_entry()) + "\n", encoding="utf-8")
        self._stub_promote(lambda *a, **k: "https://example/pr/1")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            control_rc = pi.main(self._args("--apply", "--via-pr"))
        control_out = buf.getvalue()

        broken = types.SimpleNamespace(record=lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        saved = sys.modules.get("dream_steps")
        sys.modules["dream_steps"] = broken
        self.addCleanup(lambda: sys.modules.__setitem__("dream_steps", saved) if saved is not None
                        else sys.modules.pop("dream_steps", None))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pi.main(self._args("--apply", "--via-pr"))
        self.assertEqual(rc, control_rc)
        self.assertEqual(buf.getvalue(), control_out)

    def test_an_unimportable_dream_steps_is_a_silent_no_op(self):
        saved = sys.modules.get("dream_steps")
        sys.modules["dream_steps"] = None  # forces ImportError on import
        self.addCleanup(lambda: sys.modules.__setitem__("dream_steps", saved) if saved is not None
                        else sys.modules.pop("dream_steps", None))
        self.assertEqual(self._quiet_main(self._args("--apply")), 0)
        self.assertEqual(self._ledger().get("2e"), None)

    def test_the_owner_of_2e_is_reachable_from_the_ledger_module(self):
        """dream_steps.STEPS names promote_intel.py as 2e's owner; that name must resolve."""
        import dream_steps
        meta = dream_steps.STEPS["2e"]
        self.assertEqual(meta["owner"], "promote_intel.py")
        owner = pi._repo_root() / meta["owner_dir"] / meta["owner"]
        self.assertTrue(owner.exists(), owner)


if __name__ == "__main__":
    unittest.main()
