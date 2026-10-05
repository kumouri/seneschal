#!/usr/bin/env python3
"""Tests for model_delegate.py — the force-route one-shot delegate (!opus48, !glm, !gpt…, and !fable
routed through fable_delegate unchanged).

NO real `claude`/`codex` spawns and NO network: every backend takes an injected fake — a runner with
`subprocess.run`'s signature, an opener with `urllib.request.urlopen`'s, an explicit `environ` dict for
the HuggingFace token. Stdlib ``unittest`` only.

Run:  python -m unittest seneschal.scripts.test_model_delegate
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import fable_delegate as fd  # noqa: E402
import model_config as mc  # noqa: E402
import model_delegate as md  # noqa: E402

TOKEN = "hf_SECRETtokenVALUE123"


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Runner:
    """Records every call; returns a canned result or raises."""

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return self.result


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.requests = payload, exc, []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.exc:
            raise self.exc
        return _Resp(json.dumps(self.payload).encode("utf-8"))


def _http_error(code, body):
    return urllib.error.HTTPError(md.HF_CHAT_URL, code, "err", {}, io.BytesIO(json.dumps(body).encode()))


def _codex_stdout(*events):
    return "\n".join(json.dumps(e) for e in events)


def _codex_ok(text="the answer"):
    return _codex_stdout({"type": "thread.started", "thread_id": "t"},
                         {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                         {"type": "turn.completed", "usage": {"output_tokens": 5}})


def _codex_rejected(model):
    inner = json.dumps({"type": "error", "status": 400, "error": {
        "type": "invalid_request_error",
        "message": f"The '{model}' model is not supported when using Codex with a ChatGPT account."}})
    return _codex_stdout({"type": "error", "message": inner},
                         {"type": "turn.failed", "error": {"message": inner}})


class Registry(unittest.TestCase):
    def test_every_shipped_alias_is_registered(self):
        self.assertEqual(set(md.TARGETS), {"fable", "opus48", "glm", "gpt54", "gpt56sol",
                                           "gpt6astra", "gpt6sol", "gpt61sol"})

    def test_aliases_are_lowercase_prefix_safe_tokens(self):
        for alias in md.TARGETS:
            self.assertRegex(alias, r"^[a-z0-9]+$")

    def test_backends_and_ids(self):
        self.assertEqual(md.TARGETS["fable"].model, fd.DEFAULT_MODEL)
        self.assertEqual((md.TARGETS["opus48"].backend, md.TARGETS["opus48"].model),
                         ("claude-cli", "claude-opus-4-8"))
        self.assertEqual((md.TARGETS["glm"].backend, md.TARGETS["glm"].model), ("hf", "zai-org/GLM-5.3"))
        for a in ("gpt54", "gpt56sol", "gpt6astra", "gpt6sol", "gpt61sol"):
            self.assertEqual(md.TARGETS[a].backend, "codex")

    def test_an_unverified_target_says_why(self):
        for t in md.TARGETS.values():
            if t.verified is None and t.backend != "claude-cli":
                self.assertTrue(t.note.startswith("REJECTED"), t)

    def test_opus48_is_a_ranked_claude_model(self):
        self.assertIsNotNone(mc.rank_of(md.TARGETS["opus48"].model))

    def test_resolve_tolerates_bang_and_case(self):
        self.assertIs(md.resolve("!GLM"), md.TARGETS["glm"])
        self.assertIsNone(md.resolve("nope"))


class ClaudeTarget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        mc.save(self.dir, "claude-opus-5-5", "claude-fable-5-1")

    def test_argv_uses_the_real_model_and_json_mode(self):
        runner = _Runner(_Completed(0, json.dumps({"is_error": False, "result": "hi", "usage": {}})))
        ok, out = md.delegate("opus48", "q", state_dir=self.dir, include_thread=False, runner=runner)
        self.assertEqual((ok, out), (True, "hi"))
        argv = runner.calls[0][0]
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(runner.calls[0][1]["env"].keys()))

    def test_opus48_child_is_answer_only_outside_the_repo_with_a_stripped_seed(self):
        # The recursion repro, end to end through delegate(): the seed carries the very directive that
        # force-routed this turn; the child gets it stripped, no tools, no MCP, a neutral cwd.
        import presence
        with open(os.path.join(self.dir, "telegram-thread.json"), "w", encoding="utf-8") as fh:
            json.dump([{"role": "user", "text": presence.force_route_directive("opus48")
                        + "\n\nThis is a test to see if the message gets through to Opus 4.8."}], fh)
        runner = _Runner(_Completed(0, json.dumps({"is_error": False, "result": "ok", "usage": {}})))
        ok, out = md.delegate("opus48", "This is a test to see if the message gets through to Opus 4.8.",
                              state_dir=self.dir, runner=runner)
        self.assertEqual((ok, out), (True, "ok"))
        argv, kw = runner.calls[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        prompt = argv[-1]
        self.assertNotIn("force-route", prompt)
        self.assertNotIn("model_delegate.py", prompt)
        self.assertIn("gets through to Opus 4.8.", prompt)
        repo = os.path.realpath(os.path.join(SCRIPT_DIR, "..", ".."))
        self.assertFalse(os.path.normcase(os.path.realpath(kw["cwd"])).startswith(
            os.path.normcase(repo) + os.sep))
        self.assertEqual(kw["env"][fd.DELEGATE_DEPTH_ENV], "1")

    def test_ceiling_below_the_target_refuses_without_spawning(self):
        mc.save(self.dir, "claude-haiku-4-5", "claude-sonnet-5")
        runner = _Runner(_Completed(0, "x"))
        ok, out = md.delegate("opus48", "q", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("Opus 4.8 delegation refused", out)
        self.assertIn("claude-sonnet-5", out)
        self.assertEqual(runner.calls, [])

    def test_codex_warm_backend_refuses_a_claude_target(self):
        mc.save(self.dir, "gpt-6-astra", "gpt-6-astra", backend="codex-cli")
        ok, out = md.delegate("opus48", "q", state_dir=self.dir, runner=_Runner(_Completed(0, "x")))
        self.assertFalse(ok)
        self.assertIn("codex-cli", out)

    def test_run_failure_names_the_target_not_fable(self):
        ok, out = md.delegate("opus48", "q", state_dir=self.dir, include_thread=False,
                              runner=_Runner(_Completed(1, "", "boom")))
        self.assertFalse(ok)
        self.assertTrue(out.startswith("Opus 4.8 delegation failed:"), out)
        self.assertNotIn("fable", out.lower())

    def test_fable_alias_goes_through_fable_delegate_unchanged(self):
        calls = []
        orig = fd.delegate
        fd.delegate = lambda task, **kw: calls.append((task, kw)) or (True, "fable says")
        try:
            self.assertEqual(md.delegate("fable", "q", state_dir=self.dir), (True, "fable says"))
        finally:
            fd.delegate = orig
        self.assertEqual(calls[0][0], "q")
        self.assertEqual(calls[0][1]["model"], fd.DEFAULT_MODEL)


class HfTarget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"HUGGINGFACE_API_KEY": TOKEN}

    def _ok(self, content="glm answer", finish="stop"):
        return {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": finish}], "usage": {}}

    def test_request_shape(self):
        opener = _Opener(self._ok())
        ok, out = md.delegate("glm", "the task", state_dir=self.dir, include_thread=False,
                              environ=self.env, opener=opener)
        self.assertEqual((ok, out), (True, "glm answer"))
        req, timeout = opener.requests[0]
        self.assertEqual(req.full_url, "https://router.huggingface.co/v1/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "zai-org/GLM-5.3")
        self.assertEqual(body["messages"], [{"role": "user", "content": "the task"}])
        self.assertEqual(body["max_tokens"], md.HF_DEFAULT_MAX_TOKENS)
        self.assertEqual(timeout, md.DEFAULT_TIMEOUT_SEC)

    def test_hf_token_is_the_fallback_variable(self):
        self.assertEqual(md.hf_token({"HF_TOKEN": "hf_x"}), "hf_x")
        self.assertEqual(md.hf_token({"HUGGINGFACE_API_KEY": "a", "HF_TOKEN": "b"}), "a")
        self.assertIsNone(md.hf_token({"HUGGINGFACE_API_KEY": "  "}))

    def test_missing_token_names_both_variables_and_never_calls_out(self):
        opener = _Opener(self._ok())
        ok, out = md.delegate("glm", "q", state_dir=self.dir, environ={}, opener=opener)
        self.assertFalse(ok)
        self.assertIn("HUGGINGFACE_API_KEY or HF_TOKEN", out)
        self.assertEqual(opener.requests, [])

    def test_rejected_model(self):
        err = _http_error(400, {"error": {"message": "The requested model 'zai-org/GLM-5.3' does not exist.",
                                          "code": "model_not_found"}})
        ok, out = md.delegate("glm", "q", state_dir=self.dir, environ=self.env, opener=_Opener(exc=err))
        self.assertFalse(ok)
        self.assertIn("rejected the model id 'zai-org/GLM-5.3'", out)
        self.assertIn("does not exist", out)

    def test_bad_token_and_no_credit(self):
        for code, phrase in ((401, "rejected the token"), (402, "credit is exhausted")):
            err = _http_error(code, {"error": "nope"})
            ok, out = md.delegate("glm", "q", state_dir=self.dir, environ=self.env, opener=_Opener(exc=err))
            self.assertFalse(ok)
            self.assertIn(phrase, out)

    def test_the_token_never_appears_in_a_failure_even_if_the_provider_echoes_it(self):
        err = _http_error(401, {"error": {"message": f"Invalid credentials: Bearer {TOKEN}"}})
        ok, out = md.delegate("glm", "q", state_dir=self.dir, environ=self.env, opener=_Opener(exc=err))
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, out)
        net = urllib.error.URLError(f"refused for {TOKEN}")
        ok, out = md.delegate("glm", "q", state_dir=self.dir, environ=self.env, opener=_Opener(exc=net))
        self.assertIn("couldn't reach HuggingFace", out)
        self.assertNotIn(TOKEN, out)

    def test_reasoning_ate_the_whole_budget(self):
        ok, out = md.delegate("glm", "q", state_dir=self.dir, environ=self.env,
                              opener=_Opener(self._ok(content="", finish="length")))
        self.assertFalse(ok)
        self.assertIn("reasoning", out)
        self.assertIn("--max-tokens", out)

    def test_success_badges_the_transcript_with_the_real_model(self):
        badges = []
        orig = fd.append_transcript_badge
        fd.append_transcript_badge = lambda sd, text, **kw: badges.append((text, kw.get("model")))
        try:
            md.delegate("glm", "q", state_dir=self.dir, environ=self.env, opener=_Opener(self._ok()))
        finally:
            fd.append_transcript_badge = orig
        self.assertEqual(badges, [("glm answer", "zai-org/GLM-5.3")])


class HfTargetAlternateDoor(unittest.TestCase):
    """run_hf_target's url/token_vars/require_token/provider_label generalization — the seam any other
    OpenAI-compatible door (a hosted provider, a local quant) reuses without a parallel caller."""

    def _ok(self):
        return {"choices": [{"message": {"content": "no"}, "finish_reason": "stop"}], "usage": {}}

    def test_custom_url_and_token_var_and_label(self):
        target = md.Target("hf", "cais/HarmBench-Llama-2-13b-cls", "judge", None)
        opener = _Opener(self._ok())
        ok, out = md.run_hf_target(target, "p", md.DEFAULT_TIMEOUT_SEC,
                                   environ={"FEATHERLESS_API_KEY": "fk_SECRET"}, opener=opener,
                                   url="https://api.featherless.ai/v1/chat/completions",
                                   token_vars=("FEATHERLESS_API_KEY",), provider_label="Featherless")
        self.assertEqual((ok, out), (True, "no"))
        req = opener.requests[0][0]
        self.assertEqual(req.full_url, "https://api.featherless.ai/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer fk_SECRET")

    def test_missing_token_names_the_custom_var_and_label(self):
        target = md.Target("hf", "m", "judge", None)
        ok, out = md.run_hf_target(target, "p", md.DEFAULT_TIMEOUT_SEC, environ={},
                                   opener=_Opener(self._ok()), token_vars=("FEATHERLESS_API_KEY",),
                                   provider_label="Featherless")
        self.assertFalse(ok)
        self.assertIn("Featherless", out)
        self.assertIn("FEATHERLESS_API_KEY", out)

    def test_require_token_false_sends_no_authorization_header(self):
        target = md.Target("hf", "m", "judge", None)
        opener = _Opener(self._ok())
        ok, _ = md.run_hf_target(target, "p", md.DEFAULT_TIMEOUT_SEC, environ={}, opener=opener,
                                 url="http://127.0.0.1:1234/v1/chat/completions", require_token=False,
                                 provider_label="local quant")
        self.assertTrue(ok)
        self.assertIsNone(opener.requests[0][0].get_header("Authorization"))

    def test_hf_defaults_are_unchanged(self):
        # No new kwargs => identical HF-router behavior (the GLM subject path relies on this).
        target = md.TARGETS["glm"]
        opener = _Opener(self._ok())
        md.run_hf_target(target, "p", md.DEFAULT_TIMEOUT_SEC,
                         environ={"HUGGINGFACE_API_KEY": TOKEN}, opener=opener)
        req = opener.requests[0][0]
        self.assertEqual(req.full_url, md.HF_CHAT_URL)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")


class CodexTarget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, alias, runner, logged_in=True):
        return md.delegate(alias, "the task", state_dir=self.dir, include_thread=False,
                           codex_bin="codex.exe", runner=runner, codex_logged_in_fn=lambda: logged_in)

    def test_argv_is_read_only_grant_free_and_prompt_on_stdin(self):
        runner = _Runner(_Completed(0, _codex_ok("pong")))
        self.assertEqual(self._run("gpt6sol", runner), (True, "pong"))
        argv, kw = runner.calls[0]
        self.assertEqual(argv, ["codex.exe", "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                                "--ignore-user-config", "--sandbox", "read-only", "-m", "gpt-6-sol", "-"])
        self.assertTrue(kw["input"].startswith(md.CODEX_ANSWER_ONLY))
        self.assertTrue(kw["input"].endswith("the task"))
        self.assertNotIn("the task", " ".join(argv))
        self.assertNotIn("OPENAI_API_KEY", sorted(kw["env"].keys()))
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(kw["env"].keys()))
        self.assertFalse(os.path.exists(kw["cwd"]), "the empty temp cwd is cleaned up")

    def test_each_gpt_alias_passes_its_own_model(self):
        for alias in ("gpt54", "gpt56sol", "gpt6astra", "gpt6sol", "gpt61sol"):
            runner = _Runner(_Completed(0, _codex_ok()))
            self._run(alias, runner)
            argv = runner.calls[0][0]
            self.assertEqual(argv[argv.index("-m") + 1], md.TARGETS[alias].model)

    def test_the_last_agent_message_is_the_answer(self):
        out = _codex_stdout({"type": "item.completed", "item": {"type": "agent_message", "text": "thinking"}},
                            {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}})
        self.assertEqual(md.parse_codex_events(out), ("final", None))

    def test_rejected_model_relays_codexs_own_sentence(self):
        ok, out = self._run("gpt54", _Runner(_Completed(1, _codex_rejected("gpt-5.4"))))
        self.assertFalse(ok)
        self.assertIn("Codex rejected the model id 'gpt-5.4'", out)
        self.assertIn("not supported when using Codex with a ChatGPT account", out)

    def test_not_logged_in_refuses_without_spawning(self):
        runner = _Runner(_Completed(0, _codex_ok()))
        ok, out = self._run("gpt6sol", runner, logged_in=False)
        self.assertFalse(ok)
        self.assertIn("codex login", out)
        self.assertEqual(runner.calls, [])

    def test_missing_binary_and_timeout(self):
        ok, out = self._run("gpt6sol", _Runner(exc=FileNotFoundError("codex.exe")))
        self.assertIn("isn't installed", out)
        import subprocess
        ok, out = self._run("gpt6sol", _Runner(exc=subprocess.TimeoutExpired("codex", 5)))
        self.assertIn("didn't answer within", out)

    def test_no_answer_and_nonzero_exit(self):
        ok, out = self._run("gpt6sol", _Runner(_Completed(3, "", "Reading additional input from stdin...\nboom")))
        self.assertFalse(ok)
        self.assertIn("exited 3", out)
        self.assertIn("boom", out)
        self.assertNotIn("Reading additional input", out)


class RecursionGuard(unittest.TestCase):
    """A delegate child carries SENESCHAL_DELEGATE_DEPTH; at depth >= 1 every target refuses in one line,
    before any ceiling read, token lookup or spawn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        mc.save(self.dir, "claude-opus-5-5", "claude-fable-5-1")

    def test_every_target_refuses_at_depth_one(self):
        for alias in md.TARGETS:
            runner = _Runner(_Completed(0, "should never run"))
            opener = _Opener(exc=AssertionError("no network"))
            ok, out = md.delegate(alias, "q", state_dir=self.dir, runner=runner, opener=opener,
                                  environ={fd.DELEGATE_DEPTH_ENV: "1", "HF_TOKEN": TOKEN},
                                  codex_logged_in_fn=lambda: True)
            self.assertFalse(ok, alias)
            self.assertIn("already a delegate", out)
            self.assertNotIn("\n", out)
            self.assertEqual(runner.calls, [], alias)
            self.assertEqual(opener.requests, [], alias)

    def test_depth_zero_is_unaffected(self):
        runner = _Runner(_Completed(0, json.dumps({"is_error": False, "result": "hi", "usage": {}})))
        ok, _ = md.delegate("opus48", "q", state_dir=self.dir, include_thread=False, runner=runner,
                            environ={fd.DELEGATE_DEPTH_ENV: "0"})
        self.assertTrue(ok)

    def test_codex_child_env_carries_the_depth(self):
        old = os.environ.pop(fd.DELEGATE_DEPTH_ENV, None)
        try:
            self.assertEqual(md.codex_env()[fd.DELEGATE_DEPTH_ENV], "1")
        finally:
            if old is not None:
                os.environ[fd.DELEGATE_DEPTH_ENV] = old

    def test_cli_exits_2_inside_a_delegate(self):
        old = os.environ.get(fd.DELEGATE_DEPTH_ENV)
        os.environ[fd.DELEGATE_DEPTH_ENV] = "1"
        err = io.StringIO()
        try:
            real = sys.stderr
            sys.stderr = err
            try:
                rc = md._main(["model_delegate.py", "--target", "opus48", "--state-dir", self.dir, "q"])
            finally:
                sys.stderr = real
        finally:
            if old is None:
                os.environ.pop(fd.DELEGATE_DEPTH_ENV, None)
            else:
                os.environ[fd.DELEGATE_DEPTH_ENV] = old
        self.assertEqual(rc, 2)
        self.assertIn("model_delegate refused", err.getvalue())


class Cli(unittest.TestCase):
    def test_list_and_missing_target(self):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(md._main(["model_delegate.py", "--list"]), 0)
        self.assertIn("!glm", buf.getvalue())
        self.assertIn("UNVERIFIED", buf.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(md._main(["model_delegate.py", "task"]), 2)

    def test_unknown_alias_lists_the_known_ones(self):
        ok, out = md.delegate("nope", "q")
        self.assertFalse(ok)
        self.assertIn("glm", out)



class UserAgentTest(unittest.TestCase):
    """Featherless (behind Cloudflare) 403s Python's default urllib User-Agent with "error code: 1010"
    Every OpenAI-compatible request must carry the named one."""

    def test_request_carries_named_user_agent(self):
        req = md.build_hf_request("m", "hi", "tok", url="https://api.featherless.ai/v1/chat/completions")
        self.assertEqual(req.get_header("User-agent"), md.USER_AGENT)
        self.assertNotIn("Python-urllib", req.get_header("User-agent"))

    def test_tokenless_request_still_carries_it(self):
        req = md.build_hf_request("m", "hi", "")
        self.assertEqual(req.get_header("User-agent"), md.USER_AGENT)

if __name__ == "__main__":
    unittest.main()
