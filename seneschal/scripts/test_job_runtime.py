#!/usr/bin/env python3
"""Tests for `jobs.py`'s `runtime` block (background-jobs-spec §3.16).

The cockpit has to say what jobs are on what subscription / harness / model, and which ones are
non-LLM. `start_job` stamps `runtime: {subscription, harness, model, llm}` on every record.

**The invariants pinned here:**
  * `llm` is `job_agent_class` VERBATIM — the badge and the caps can never disagree about a job.
  * `model` is NEVER GUESSED: the argv's own flag, a delegate target's registered id, or the literal
    "default" when an agent harness ran with no model flag. An agent job whose harness the argv does
    not name is `harness: "unknown"` with no subscription and no model.
  * `subscription` is the login the child will ACTUALLY use — `--account`, else the inherited
    `CLAUDE_CONFIG_DIR` (named from the registry when it can be), else "main".
  * STAMPED AT START, READ LATER: an old record without the block renders "—" and is never re-derived.

Run: python -m unittest seneschal.scripts.test_job_runtime   (or)   python test_job_runtime.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import claude_accounts as ca  # noqa: E402
import jobs  # noqa: E402
import model_delegate  # noqa: E402
from test_jobs import NOW, _runner  # noqa: E402 — shared seams


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        # No test here may read the host's real login dir or inherit the runner's CLAUDE_CONFIG_DIR.
        self.default_login = os.path.join(self.tmp.name, "default-login")
        p = mock.patch.object(ca, "default_config_dir", return_value=self.default_login)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def rt(self, argv, **rec):
        return jobs.job_runtime({"argv": argv, **rec}, self.state)


class RuntimeDerivationTests(_StateCase):
    def test_bare_claude_is_main_claude_cli_default(self):
        self.assertEqual(self.rt(["claude", "-p", "brief"]),
                         {"subscription": "main", "harness": "claude-cli", "model": "default",
                          "llm": True})

    def test_claude_model_flag_is_read_both_spellings(self):
        self.assertEqual(self.rt(["claude", "-p", "x", "--model", "claude-opus-5-5"])["model"],
                         "claude-opus-5-5")
        self.assertEqual(self.rt(["C:\\bin\\claude.exe", "--model=claude-sonnet-5-5", "-p", "x"])
                         ["model"], "claude-sonnet-5-5")

    def test_a_wrapped_claude_is_still_claude_cli(self):
        rt = self.rt(["wsl", "--", "claude", "-p", "x", "--model", "claude-haiku-4-5"])
        self.assertEqual((rt["harness"], rt["model"]), ("claude-cli", "claude-haiku-4-5"))

    def test_a_prompt_that_mentions_claude_is_not_a_harness(self):
        rt = self.rt(["pwsh", "-File", "x.ps1", "ask claude about it"])
        self.assertEqual(rt["harness"], "unknown")

    def test_codex_is_chatgpt(self):
        self.assertEqual(self.rt(["codex", "exec", "-m", "gpt-6-sol", "-"]),
                         {"subscription": "ChatGPT", "harness": "codex", "model": "gpt-6-sol",
                          "llm": True})
        self.assertEqual(self.rt(["codex", "exec", "-"])["model"], "default")

    def test_model_delegate_reads_the_target_registry(self):
        glm = model_delegate.TARGETS["glm"]
        rt = self.rt(["python", "seneschal/scripts/model_delegate.py", "--target", "glm", "q"])
        self.assertEqual(rt, {"subscription": "HuggingFace", "harness": "hf-router",
                              "model": glm.model, "llm": True})
        rt = self.rt(["python", "model_delegate.py", "--target", "gpt6sol", "q"])
        self.assertEqual((rt["subscription"], rt["harness"], rt["model"]),
                         ("ChatGPT", "codex", model_delegate.TARGETS["gpt6sol"].model))
        rt = self.rt(["python", "model_delegate.py", "--target=opus48", "q"])
        self.assertEqual((rt["subscription"], rt["harness"], rt["model"]),
                         ("main", "claude-cli", "claude-opus-4-8"))

    def test_an_unresolvable_delegate_target_is_unknown_not_guessed(self):
        rt = self.rt(["python", "model_delegate.py", "--target", "nope", "q"])
        self.assertEqual((rt["harness"], rt["model"], rt["subscription"]), ("unknown", None, None))

    def test_fable_delegate_uses_its_own_default_model(self):
        import fable_delegate
        rt = self.rt(["python", "fable_delegate.py", "task"])
        self.assertEqual((rt["harness"], rt["model"], rt["llm"]),
                         ("claude-cli", fable_delegate.DEFAULT_MODEL, True))

    def test_non_agent_job_is_none_with_no_subscription_or_model(self):
        self.assertEqual(self.rt(["ffmpeg", "-i", "a.mp4", "b.mp3"]),
                         {"subscription": None, "harness": "none", "model": None, "llm": False})

    def test_unknown_class_is_unknown_harness(self):
        self.assertEqual(self.rt(["bash", "x.sh"]),
                         {"subscription": None, "harness": "unknown", "model": None,
                          "llm": "unknown"})

    def test_llm_is_job_agent_class_verbatim_including_the_stamp(self):
        """The badge and the caps must never disagree — so the stamp wins here exactly as it does there."""
        for argv, agent in ((["claude", "-p", "x"], False), (["bash", "x.sh"], True),
                            (["ffmpeg"], None), (["bash", "x.sh"], None)):
            rec = {"argv": argv} if agent is None else {"argv": argv, "agent": agent}
            cls = jobs.job_agent_class(rec)[0]
            want = {jobs.AGENT: True, jobs.NON_AGENT: False}.get(cls, "unknown")
            self.assertEqual(jobs.job_runtime(rec, self.state)["llm"], want, (argv, agent))
        self.assertEqual(self.rt(["claude", "-p", "x"], agent=False)["harness"], "none")

    def test_an_agent_script_with_no_known_harness_says_unknown(self):
        rt = self.rt(["python", "rag_index.py"])
        self.assertEqual((rt["llm"], rt["harness"], rt["model"]), (True, "unknown", None))

    def test_ollama_is_local(self):
        rt = self.rt(["ollama", "run", "qwen3:14b", "hi"])
        self.assertEqual((rt["subscription"], rt["harness"], rt["model"]),
                         ("local", "ollama", "qwen3:14b"))

    def test_a_malformed_record_never_raises(self):
        for rec in ({}, {"argv": None}, {"argv": [object()]}, "junk"):
            rt = jobs.job_runtime(rec, self.state)
            self.assertIn(rt["llm"], (True, False, "unknown"))


class SubscriptionTests(_StateCase):
    def _registry(self, data):
        with open(os.path.join(self.state, ca.REGISTRY_FILE), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_account_flag_names_the_subscription_and_default_is_main(self):
        self.assertEqual(self.rt(["claude"], account={"name": "2", "config_dir": "C:\\x"})
                         ["subscription"], "2")
        self.assertEqual(self.rt(["claude"], account={"name": "default", "config_dir": None})
                         ["subscription"], "main")

    def test_an_inherited_config_dir_is_named_from_the_registry(self):
        carbon = os.path.join(self.tmp.name, ".claude-carbon")
        self._registry({"default": None, "carbon": carbon})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": carbon}):
            self.assertEqual(self.rt(["claude"])["subscription"], "carbon")

    def test_an_unregistered_inherited_dir_is_its_basename_never_a_guess(self):
        other = os.path.join(self.tmp.name, ".claude-mystery")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": other}):
            self.assertEqual(self.rt(["claude"])["subscription"], ".claude-mystery")

    def test_inheriting_the_default_dir_is_main(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.default_login}):
            self.assertEqual(self.rt(["claude"])["subscription"], "main")

    def test_a_broken_registry_does_not_cost_the_start(self):
        with open(os.path.join(self.state, ca.REGISTRY_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        other = os.path.join(self.tmp.name, ".claude-2")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": other}):
            self.assertEqual(self.rt(["claude"])["subscription"], ".claude-2")


class StampAndDisplayTests(_StateCase):
    def _start(self, argv, **kw):
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "t", argv, runner=_runner(calls=[]), **kw)

    def test_start_job_stamps_the_block_on_disk(self):
        rec = self._start(["claude", "-p", "brief", "--model", "claude-opus-5-5"],
                          account={"name": "2", "config_dir": "C:\\x"})
        on_disk = jobs.load_job(self.state, rec["id"])
        self.assertEqual(on_disk["runtime"], {"subscription": "2", "harness": "claude-cli",
                                              "model": "claude-opus-5-5", "llm": True})

    def test_the_no_agent_stamp_reaches_the_block(self):
        rec = self._start(["claude", "-p", "x"], agent=False)
        self.assertIs(rec["runtime"]["llm"], False)

    def test_labels(self):
        self.assertEqual(jobs.runtime_label({}), "—")
        self.assertEqual(jobs.runtime_label({"runtime": "junk"}), "—")
        self.assertEqual(jobs.runtime_label({"runtime": self.rt(["ffmpeg"])}), "non-LLM")
        self.assertEqual(jobs.runtime_label({"runtime": self.rt(["bash", "x.sh"])}), "LLM unknown")
        self.assertEqual(jobs.runtime_label({"runtime": self.rt(["claude"])}),
                         "main · claude-cli · default")
        self.assertEqual(jobs.runtime_label({"runtime": self.rt(["python", "rag_index.py"])}),
                         "LLM · harness unknown")

    def test_an_old_record_is_never_re_derived(self):
        """A record written before the block existed reads "—", even though its argv WOULD classify."""
        rec = self._start(["claude", "-p", "x"])
        del rec["runtime"]
        jobs.save_job(self.state, rec)
        self.assertIsNone(jobs.runtime_of(jobs.load_job(self.state, rec["id"])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.main(["--state-dir", self.state, "list"])
            jobs.main(["--state-dir", self.state, "status", rec["id"]])
        text = out.getvalue()
        self.assertIn("[—]", text)
        self.assertIn("runtime: — (record predates", text)
        self.assertNotIn("claude-cli", text)

    def test_list_and_status_print_the_block(self):
        rec = self._start(["ffmpeg", "-i", "a"])
        rec2 = self._start(["codex", "exec", "-m", "gpt-6-sol", "-"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.main(["--state-dir", self.state, "list"])
            jobs.main(["--state-dir", self.state, "status", rec2["id"]])
        text = out.getvalue()
        self.assertIn("[non-LLM]", text)
        self.assertIn("[ChatGPT · codex · gpt-6-sol]", text)
        self.assertIn("runtime: subscription ChatGPT · harness codex · model gpt-6-sol · LLM yes",
                      text)
        self.assertTrue(rec["id"])

    def test_a_non_default_account_is_shown_once_not_twice(self):
        self._start(["claude", "-p", "x"], account={"name": "2", "config_dir": "C:\\x"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.main(["--state-dir", self.state, "list"])
        self.assertIn("[2 · claude-cli · default]", out.getvalue())
        self.assertNotIn("account 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
