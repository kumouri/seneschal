#!/usr/bin/env python3
"""model_delegate.py — the force-route one-shot delegate: send ONE turn to a named model on any of
three backends. Stdlib only. Generalizes `fable_delegate.py` (which stays, CLI and all — the `fable`
target below dispatches straight into it, so `!fable` is byte-for-byte what it always was).

The point: an individual turn can go to a model other than the warm session's own — another Claude
tier, an open-weights model through the HuggingFace router, or a GPT model on a ChatGPT subscription
through the Codex CLI — when the owner names it with a `!<alias>` prefix.

**`TARGETS` is the one registry.** alias -> backend + model id + label. `presence.py` builds its
`!<alias>` force-route prefix regex from these keys, so adding a target here is the whole change on
the daemon side too. Three backends:

  * `claude-cli` — `claude -p --model <id> --output-format json`, via `fable_delegate.run_claude`
    (same `ANTHROPIC_API_KEY` scrub, subscription-billed), ANSWER-ONLY: `--tools ""`
    + `--strict-mcp-config`, a neutral cwd outside the repo, a system note naming it the delegate.
  * `hf` — the HuggingFace Inference Providers OpenAI-compatible router,
    `POST https://router.huggingface.co/v1/chat/completions`, urllib only. Token from
    `HUGGINGFACE_API_KEY` (or `HF_TOKEN`), NEVER printed, logged, or echoed in a failure message —
    `_scrub` strips it from anything built out of a provider error body. `run_hf_target`/
    `build_hf_request` take an optional `url`/`token_vars`/`provider_label` (all defaulting to the HF
    router, so every existing caller is unchanged) so ANY OpenAI-compatible `/chat/completions` door is
    a drop-in (another hosted provider, a local quant).
  * `codex` — `codex exec --json --ephemeral --ignore-user-config --sandbox read-only -m <id> -` in an
    empty temp directory, prompt on stdin. `--ignore-user-config` is what makes this **grant-free**:
    Codex's only MCP source is `~/.codex/config.toml` (see `backends/codex_cli.py`'s safety note), and
    this flag skips it while auth still reads `CODEX_HOME`. Read-only sandbox + empty cwd + an
    answer-only preamble = a model answering a question, not an agent in the repo. `OPENAI_API_KEY` is
    scrubbed so a stray key can never switch the ChatGPT-subscription login to metered API.

Every target: the same budget-bounded thread seed (`fable_delegate.thread_seed`, force-route /
force-fable / router-hint directives stripped out), and the same cockpit transcript badge, carrying the
REAL model id that answered.

**A delegate never re-delegates** (an `!opus48` child, seeded with its own force-route directive,
has tried to run this script again). Besides the stripped seed and the answer-only child, every
delegate child carries `SENESCHAL_DELEGATE_DEPTH` (`fable_delegate.child_env` / `codex_env`), and
`delegate()` refuses in one line at depth >= 1 — `fable_delegate.recursion_refusal`.

**The ceiling (`state/model-config.json`'s `max_routable_model`) governs CLAUDE models only.**
  * `fable` — `fable_delegate.delegate` enforces it exactly as before (plus its live-backend check and
    the retired-but-in-place governor call).
  * other `claude-cli` targets (`opus48`) — refused unless the target's rank is at or below the
    ceiling's, and refused (same reason as Fable) when the live warm backend isn't claude-cli, since
    the stored ceiling is then a codex id and can't be compared. Opus 4.8 ranks below every ceiling a
    real config holds, so in practice this refuses only on the backend mismatch.
  * **non-Claude targets (`glm`, `gpt*`) are outside the ceiling by construction**: they are reachable
    ONLY by an explicit force-route the owner typed — the router classifier never proposes them, the fable
    arm doesn't know they exist, and nothing in the warm session's grounding tells it to reach for one
    unprompted. The ceiling is a policy about which Claude tier a turn may escalate to; an explicit
    per-turn instruction naming a different vendor's model is not an escalation it was written for.

**Force-route bypasses the router classifier, NEVER the approval gate.** This script only answers a
question; the warm session relays the answer and still drafts-and-holds anything outbound/destructive
in its own reply.

**Not metered in the governor ledger** (a named residual): only the `fable` target writes a
`fable_oneshot` spend row (inside `fable_delegate`). A new `kind` would roll up in `governor.rollups`
in ways nobody has specified; the retired governor makes that harmless today.

**Verified ids.** `verified` holds the date a one-token live call against that model id succeeded when
the target was added, or None — and `note` says what happened when it didn't. An unverified target
stays registered and fails with the provider's own rejection text rather than a guess. `--list` prints
the table; a provider can retire or rename an id at any time, so a failure names the id it tried.

Usage:
  python model_delegate.py --target glm "the task"
  echo "the task" | python model_delegate.py --target gpt6sol
  python model_delegate.py --list

Exit codes: 0 = success (answer on stdout); 2 = refused or failed (complete message on stderr, written
to be relayed to the owner as-is).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import fable_delegate  # noqa: E402 -- thread seed, claude -p runner, transcript badge, and the fable target itself
import model_config  # noqa: E402

DEFAULT_STATE_DIR = fable_delegate.DEFAULT_STATE_DIR
DEFAULT_TIMEOUT_SEC = 600
HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
HF_TOKEN_VARS = ("HUGGINGFACE_API_KEY", "HF_TOKEN")
# GLM-5.3 is a reasoning model: a 16-token probe spent all 16 on `reasoning_content` and returned an
# empty `content` (finish_reason "length"). The budget has to cover reasoning AND the answer.
HF_DEFAULT_MAX_TOKENS = 8192
# Where the Codex installer puts it on Windows (CODEX_SETUP.md); used only when `codex` isn't on PATH.
CODEX_WINDOWS_DEFAULT = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "OpenAI",
                                     "Codex", "bin", "codex.exe")
CODEX_ANSWER_ONLY = (
    "Answer the request below directly, in plain text. Do not run shell commands, read files, or edit "
    "anything — this is a one-shot question, not a coding task.\n\n"
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Target:
    backend: str          # "claude-cli" | "hf" | "codex"
    model: str            # the id the backend is actually called with
    label: str            # what the owner and the transcript call it
    verified: str | None  # date of a successful live one-token call, else None
    note: str = ""


TARGETS: dict[str, Target] = {
    "fable": Target("claude-cli", fable_delegate.DEFAULT_MODEL, "Fable 5", None,
                    "routed through fable_delegate.delegate unchanged"),
    "opus48": Target("claude-cli", "claude-opus-4-8", "Opus 4.8", "2026-09-30"),
    "glm": Target("hf", "zai-org/GLM-5.3", "GLM-5.3", "2026-09-30",
                  "HuggingFace Inference Providers router"),
    "gpt54": Target("codex", "gpt-5.4", "GPT-5.4", None,
                    "REJECTED 2026-09-30: \"The 'gpt-5.4' model is not supported when using Codex "
                    "with a ChatGPT account.\" Not in ~/.codex/models_cache.json either. Re-checked "
                    "2026-09-30 on Codex 0.159.2: still rejected."),
    "gpt56sol": Target("codex", "gpt-5.6-sol", "GPT-5.6-Sol", "2026-09-30"),
    "gpt6astra": Target("codex", "gpt-6-astra", "GPT-6-Astra", "2026-09-30"),
    "gpt6sol": Target("codex", "gpt-6-sol", "GPT-6-Sol", "2026-09-30"),
    "gpt61sol": Target("codex", "gpt-6.1-sol", "GPT-6.1-Sol", "2026-09-30",
                       "rejected on Codex 0.155.1; works from Codex 0.159.2"),
}


def resolve(alias: str) -> Target | None:
    return TARGETS.get((alias or "").strip().lower().lstrip("!"))


# --------------------------------------------------------------------------- claude-cli (non-Fable)

def claude_ceiling_refusal(target: Target, state_dir: str) -> str | None:
    """None when the live max-routable ceiling admits `target` (a claude-cli model); else the complete
    refusal message. Fable's own ceiling check stays inside `fable_delegate.delegate`."""
    cfg = model_config.load(state_dir)
    backend = cfg.get("backend") or model_config.DEFAULT_BACKEND
    ceiling = cfg.get("max_routable_model")
    if backend != "claude-cli":
        return (f"{target.label} delegation refused: the live warm backend is {backend!r}, so "
                "state/model-config.json's max_routable_model is a codex id and can't govern a Claude "
                "model. Switch the cockpit's backend dial back to claude-cli, or handle this turn "
                "yourself.")
    want = model_config.rank_of(target.model)
    cap = model_config.rank_of(ceiling)
    if want is None or cap is None or want > cap:
        return (f"{target.label} delegation refused: state/model-config.json's max_routable_model "
                f"({ceiling!r}) doesn't admit {target.model}. Raise the ceiling in the cockpit's Model "
                "dials panel before this target can run — for this turn, handle it yourself.")
    return None


def run_claude_target(target: Target, prompt: str, claude_bin: str, timeout: int,
                      runner=subprocess.run, prompt_on_stdin: bool = False) -> tuple[bool, str]:
    ok, out, _usage = fable_delegate.run_claude(prompt, target.model, claude_bin, timeout, runner=runner,
                                                prompt_on_stdin=prompt_on_stdin)
    if not ok:
        # run_claude's messages say "fable delegate"; name the real target instead.
        return False, f"{target.label} delegation failed: " + out.replace("fable delegate ", "", 1)
    return True, out


# --------------------------------------------------------------------------- hf (HuggingFace router)

def hf_token(environ=None, token_vars=HF_TOKEN_VARS) -> str | None:
    """First non-empty of `token_vars` in the environment. `token_vars` defaults to HuggingFace's own
    pair, so every existing caller is unchanged; a caller routing through a non-HF OpenAI-compatible door
    passes its own tuple (e.g. `("FEATHERLESS_API_KEY",)`)."""
    env = os.environ if environ is None else environ
    for name in token_vars:
        v = (env.get(name) or "").strip()
        if v:
            return v
    return None


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


USER_AGENT = "seneschal-model-delegate/1.0"


def build_hf_request(model: str, prompt: str, token: str,
                     max_tokens: int = HF_DEFAULT_MAX_TOKENS,
                     temperature: float | None = None,
                     url: str = HF_CHAT_URL) -> urllib.request.Request:
    """`temperature` is omitted from the body when None (the provider default), so an existing caller
    is byte-for-byte unchanged; a caller that needs sampling to be a recorded parameter pins it. `url`
    defaults to the HF router; any OpenAI-compatible `/chat/completions` endpoint (another hosted
    provider, a local quant) is a drop-in. The Authorization header is omitted when `token` is
    empty, so an auth-less local endpoint works."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode("utf-8")
    # A named User-Agent, always: Featherless sits behind Cloudflare, which answers Python's default
    # "Python-urllib/3.x" with HTTP 403 "error code: 1010" (a browser-signature block, NOT an auth or
    # gating failure) -- seen as a 403 on a valid key. Harmless elsewhere.
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=body, method="POST", headers=headers)


def _hf_error_message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body.decode("utf-8", "replace")[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err or data)[:300]


def run_hf_target(target: Target, prompt: str, timeout: int, environ=None,
                  opener=urllib.request.urlopen,
                  max_tokens: int = HF_DEFAULT_MAX_TOKENS,
                  temperature: float | None = None,
                  url: str = HF_CHAT_URL, token_vars=HF_TOKEN_VARS,
                  require_token: bool = True,
                  provider_label: str = "HuggingFace") -> tuple[bool, str]:
    """One chat-completions call. `opener` is swappable (tests pass a fake with urlopen's signature) —
    no network in tests. Never raises; never lets the token into the returned message. `temperature`
    is forwarded to the request body only when set (else provider default).

    `url`/`token_vars`/`require_token`/`provider_label` default to the HuggingFace router, so every
    existing caller (GLM-5.3 subject generation included) is byte-for-byte unchanged. A non-HF
    OpenAI-compatible door — another hosted provider, a local quant — passes
    its own `url` + `token_vars` + label; `require_token=False` lets an auth-less local endpoint run
    without a token. The provider's own error body is still appended verbatim (scrubbed), so a door's
    failure names its own fix (e.g. Featherless's gated-model 403 says which account step to take)."""
    token = hf_token(environ, token_vars)
    if not token and require_token:
        return False, (f"{target.label} delegation failed: no {provider_label} token in the daemon's "
                       f"environment ({' or '.join(token_vars)} is unset). Set it where the daemon "
                       "starts and restart it — for this turn, handle it yourself.")
    req = build_hf_request(target.model, prompt, token or "", max_tokens, temperature, url=url)
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = _scrub(_hf_error_message(e.read()), token)
        except Exception:  # noqa: BLE001
            detail = ""
        finally:
            e.close()
        if e.code in (401, 403):
            why = f"{provider_label} rejected the token (HTTP {e.code}) — it may be expired, lack " \
                  "permission, or (403) the model may be gated for this account"
        elif e.code == 402:
            why = f"{provider_label} says the account credit is exhausted (HTTP 402)"
        elif e.code in (400, 404) and "model" in detail.lower():
            why = f"{provider_label} rejected the model id {target.model!r} (HTTP {e.code})"
        else:
            why = f"{provider_label} returned HTTP {e.code}"
        return False, f"{target.label} delegation failed: {why}: {detail or 'no detail'}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, _scrub(f"{target.label} delegation failed: couldn't reach {provider_label} "
                             f"({type(e).__name__}: {e}).", token)
    except ValueError:
        return False, f"{target.label} delegation failed: {provider_label} returned a non-JSON response."
    try:
        choice = data["choices"][0]
        content = (choice.get("message") or {}).get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return False, _scrub(f"{target.label} delegation failed: unexpected response shape: "
                             f"{str(data)[:300]}", token)
    content = content.strip()
    if not content:
        if choice.get("finish_reason") == "length":
            return False, (f"{target.label} delegation failed: it spent its whole {max_tokens}-token "
                           "budget reasoning and returned no answer. Retry with a larger --max-tokens "
                           "or a narrower task.")
        return False, f"{target.label} delegation failed: the model returned an empty answer."
    return True, content


# --------------------------------------------------------------------------- codex (ChatGPT subscription)

def codex_bin_default() -> str:
    env = os.environ.get("CODEX_BIN")
    if env:
        return env
    found = shutil.which("codex")
    if found:
        return found
    return CODEX_WINDOWS_DEFAULT if os.path.exists(CODEX_WINDOWS_DEFAULT) else "codex"


def codex_env() -> dict:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env[fable_delegate.DELEGATE_DEPTH_ENV] = str(fable_delegate.delegate_depth() + 1)
    return env


def build_codex_argv(codex_bin: str, model: str) -> list[str]:
    """The prompt goes on stdin (`-`), not argv: a Windows command line caps at 32,767 chars and a
    seeded prompt can get close; it also keeps the owner's text out of the process table."""
    return [codex_bin, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config", "--sandbox", "read-only", "-m", model, "-"]


def _innermost_message(raw) -> str:
    """Codex nests the API error as a JSON string inside `message`; unwrap to the human sentence."""
    msg = raw
    for _ in range(3):
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("error") or msg
            if isinstance(msg, dict) and "message" not in msg and "error" not in msg:
                break
            continue
        if isinstance(msg, str):
            try:
                msg = json.loads(msg)
                continue
            except ValueError:
                break
        break
    return str(msg)[:400]


def parse_codex_events(stdout: str) -> tuple[str | None, str | None]:
    """(answer, error) from `codex exec --json` JSONL: the LAST `agent_message` is the answer; the first
    `turn.failed`/`error` event is the error."""
    answer, error = None, None
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                answer = item["text"]
        elif t in ("turn.failed", "error", "thread.error") and error is None:
            error = _innermost_message(ev.get("error") or ev.get("message") or ev)
    return answer, error


def codex_logged_in() -> bool:
    try:
        from backends import codex_cli  # lazy: only the codex path needs it
        return codex_cli.codex_identity() is not None
    except Exception:  # noqa: BLE001 -- can't tell != not logged in; let the run itself say
        return True


def run_codex_target(target: Target, prompt: str, codex_bin: str, timeout: int,
                     runner=subprocess.run, logged_in=codex_logged_in) -> tuple[bool, str]:
    if not logged_in():
        return False, (f"{target.label} delegation failed: Codex isn't logged in on this host (no "
                       "ChatGPT account in ~/.codex/auth.json). The owner needs to run `codex login` once "
                       "(seneschal/scripts/CODEX_SETUP.md) — for this turn, handle it yourself.")
    cwd = tempfile.mkdtemp(prefix="seneschal-codex-delegate-")
    try:
        proc = runner(build_codex_argv(codex_bin, target.model), input=CODEX_ANSWER_ONLY + prompt,
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=timeout, env=codex_env(), cwd=cwd, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return False, (f"{target.label} delegation failed: the Codex CLI isn't installed at "
                       f"{codex_bin!r} (set CODEX_BIN or see seneschal/scripts/CODEX_SETUP.md).")
    except subprocess.TimeoutExpired:
        return False, f"{target.label} delegation failed: Codex didn't answer within {timeout}s."
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{target.label} delegation failed to run Codex: {e}"
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    answer, error = parse_codex_events(proc.stdout)
    if error:
        low = error.lower()
        if "not supported" in low or ("model" in low and ("invalid" in low or "not found" in low)):
            return False, (f"{target.label} delegation failed: Codex rejected the model id "
                           f"{target.model!r} — {error}")
        if "401" in low or "unauthorized" in low or "log in" in low or "login" in low:
            return False, (f"{target.label} delegation failed: Codex's ChatGPT login was refused — "
                           f"{error}. The owner needs to run `codex login` again.")
        return False, f"{target.label} delegation failed: {error}"
    if proc.returncode != 0 or not answer:
        err = (proc.stderr or "").replace("Reading additional input from stdin...", "").strip()[:400]
        return False, (f"{target.label} delegation failed: codex exited {proc.returncode} with no "
                       f"answer{': ' + err if err else ''}.")
    return True, answer.strip()


# --------------------------------------------------------------------------- dispatch

def delegate(alias: str, task: str, state_dir: str = DEFAULT_STATE_DIR, claude_bin: str = "claude",
             codex_bin: str | None = None, timeout: int = DEFAULT_TIMEOUT_SEC,
             include_thread: bool = True, runner=subprocess.run, opener=urllib.request.urlopen,
             environ=None, codex_logged_in_fn=codex_logged_in,
             max_tokens: int = HF_DEFAULT_MAX_TOKENS) -> tuple[bool, str]:
    """Route one task to the target named by `alias`. Returns (ok, message) — the answer on success, a
    complete relay-ready message otherwise. Never raises. Refuses first, before any lookup, when this
    process is itself a delegate (`SENESCHAL_DELEGATE_DEPTH` >= 1)."""
    refusal = fable_delegate.recursion_refusal("model_delegate", environ)
    if refusal:
        return False, refusal
    target = resolve(alias)
    if target is None:
        return False, (f"unknown force-route target {alias!r}; known: "
                       + ", ".join(sorted(TARGETS)) + ".")
    if alias.strip().lower().lstrip("!") == "fable":
        return fable_delegate.delegate(task, state_dir=state_dir, model=target.model,
                                       claude_bin=claude_bin, timeout=timeout,
                                       include_thread=include_thread, runner=runner)
    seed = fable_delegate.thread_seed(state_dir) if include_thread else ""
    prompt = f"{seed}{task}".strip()
    try:
        if target.backend == "claude-cli":
            refusal = claude_ceiling_refusal(target, state_dir)
            if refusal:
                return False, refusal
            ok, out = run_claude_target(target, prompt, claude_bin, timeout, runner=runner)
        elif target.backend == "hf":
            ok, out = run_hf_target(target, prompt, timeout, environ=environ, opener=opener,
                                    max_tokens=max_tokens)
        elif target.backend == "codex":
            ok, out = run_codex_target(target, prompt, codex_bin or codex_bin_default(), timeout,
                                       runner=runner, logged_in=codex_logged_in_fn)
        else:
            return False, f"{target.label} delegation failed: unknown backend {target.backend!r}."
    except Exception as e:  # noqa: BLE001 -- never raise past here; the warm session relays this
        return False, f"{target.label} delegation failed unexpectedly: {type(e).__name__}: {e}"
    if ok:
        fable_delegate.append_transcript_badge(state_dir, out, model=target.model)
    return ok, out


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="One-shot delegate a task to a force-route target.")
    p.add_argument("task", nargs="?", default=None, help="the task/prompt text (else read from stdin)")
    p.add_argument("--target", help="alias from TARGETS (e.g. glm, opus48, gpt6sol)")
    p.add_argument("--list", action="store_true", help="print the registry and exit")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--codex-bin", default=None)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--max-tokens", type=int, default=HF_DEFAULT_MAX_TOKENS,
                   help="hf backend only: completion budget, reasoning included")
    p.add_argument("--no-thread", action="store_true", help="skip seeding the conversation tail")
    args = p.parse_args(argv[1:])

    if args.list:
        for alias, t in TARGETS.items():
            status = (f"verified {t.verified}" if t.verified
                      else "UNVERIFIED" if t.note.startswith("REJECTED") else "not probed")
            print(f"!{alias:<10} {t.backend:<10} {t.model:<18} {t.label:<12} {status}"
                  + (f" — {t.note}" if t.note else ""))
        return 0
    if not args.target:
        print("model_delegate: --target is required (see --list)", file=sys.stderr)
        return 2
    task = args.task if args.task is not None else sys.stdin.read()
    task = (task or "").strip()
    if not task:
        print("model_delegate: no task text given (positional arg or stdin)", file=sys.stderr)
        return 2
    ok, out = delegate(args.target, task, state_dir=args.state_dir, claude_bin=args.claude_bin,
                       codex_bin=args.codex_bin, timeout=args.timeout,
                       include_thread=not args.no_thread, max_tokens=args.max_tokens)
    if not ok:
        print(out, file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
