#!/usr/bin/env python3
"""Fresh-eyes analysis of a failed job — the `seneschal.job-leads/1` artifact, its strict validator, the
two-scalar delegation the analyst is given, and the executor that runs one. Stdlib only.

Design: `seneschal/docs/job-origin-routing-spec.md` (phases 1-2). Parent: `background-jobs-spec.md`.

**Why an analyst at all.** A job that fails is read by the session that started it, which has a
hypothesis it is attached to and will narrate around the anomaly rather than see it. A second pair of
eyes with no sunk cost in that hypothesis notices the thing the first pair explained away.

**The payload contract, and why it lives in a function signature.** The analysis is valuable
*because* the analyst is ignorant of the working agent's reasoning. That ignorance is the product,
not a limitation to engineer away — so this module is built against the obvious, well-meant future
"improvement": *"the analyst would do better if we gave it the context we already have."* That
converts a fresh set of eyes into a slower copy of the agent that is already stuck, and it looks like
a kindness while doing it.

    def build_delegation(artifact_path: str, goal: str) -> str

**Two parameters, both scalars, one fixed template.** There is no argument through which a
transcript, a hypothesis, a prior diagnosis or the job's own `attempts[]` narrative can arrive, so
the contract cannot be violated by forgetting it — only by editing the signature, which is a
reviewable act. (`attempts[]` deserves its own sentence, because it looks harmless: it is
mechanically recorded fact, but it carries `classification: "transient"`, which is `jobs.py`'s
*verdict*, and handing that over pre-frames the search — "this was an API blip" — exactly as a human
hypothesis would.) A contract that lives only in a prompt erodes quietly; a contract that lives in a
signature is kept by construction.

**The cut that makes this coherent: ignorance is about what we PUSH, not about what exists.** The
analyst may read anything on disk — the full record, the log, every sibling job, the succeeding run
next door. Nothing is hidden. What it is not given is *a starting point that isn't its own*. Pull is
fine; push is contamination.

**The answer schema makes "point, don't diagnose" structurally impossible, not merely requested.**

  * There is **no `cause` field. No `fix`, no `patch`, no `root_cause`, no `confidence`.** The
    absence is the mechanism: an analyst inclined to diagnose has nowhere to put it.
  * **Unknown keys are REJECTED, not ignored.** A returned `"cause"` doesn't get silently dropped —
    it makes the artifact invalid, which makes the delegation a failed one. Tolerating the extra key
    would teach the analyst that the rail is decorative.
  * **`basis` is a required enum and the ordering is ENFORCED** — every `established` lead sorts
    before every `conjecture` one — rather than trusting the model to have ordered them. An
    `established` lead must carry a non-empty `cited`: "I verified this" is only meaningful with what
    was read attached.
  * **`contamination_present` is required.** If the delegation carried someone else's reasoning, the
    analyst says so at the top and ignores it. Making the field mandatory means the routing can
    *count* contamination events instead of hoping they don't happen.

**An empty list is a valid answer.** `"leads": []` is valid, cheap and correct when nothing stands
out; the validator must not treat it as failure, the executor must not retry on it, and the push must
report it plainly. This is the mirror of a lesson `references/archons.md` records — a zero exit from
an archon delegation is NOT success, because an archon can return prose and stage nothing.
Both halves are the same rule: **judge the artifact, not the exit code.** A well-formed empty
artifact is success; a zero exit with no artifact is failure. Manufacturing leads to avoid an empty
list is strictly worse than silence, because someone spends real time testing a fabricated one.

**The validator ships before the producer, deliberately** — a schema written after the first artifact
is shaped to accommodate whatever the model happened to emit.

USAGE:
  python job_analysis.py validate <artifact.json>
  python job_analysis.py delegation --artifact <path> --goal "<one line>"
  python job_analysis.py run --job <job-id> --goal "<one line>"   # what an analysis JOB runs
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import jobs  # the record store, the jobs dir, and one_line's single cap  # noqa: E402
from sentinel import parse_iso, save_json  # noqa: E402

LEADS_SCHEMA = "seneschal.job-leads/1"

BASIS_ESTABLISHED = "established"
BASIS_CONJECTURE = "conjecture"
BASES = (BASIS_ESTABLISHED, BASIS_CONJECTURE)

# The whole surface. Anything else is a REJECTION, not a dropped key — see the module docstring.
ARTIFACT_REQUIRED = ("schema", "job_id", "analyst", "produced_at", "contamination_present", "leads")
ARTIFACT_OPTIONAL = ("notes",)
ARTIFACT_KEYS = frozenset(ARTIFACT_REQUIRED + ARTIFACT_OPTIONAL)

LEAD_REQUIRED = ("rank", "location", "evidence", "basis")
LEAD_OPTIONAL = ("cited",)
LEAD_KEYS = frozenset(LEAD_REQUIRED + LEAD_OPTIONAL)

# A dedicated analyst (e.g. an archon with its own charter) would add ONE entry here, gated on that
# archon's record saying admitted. A charter that has not passed its evals is not a posture, it is a
# hope, so until then the option does not exist and `analyst_argv` refuses it loudly rather than
# falling back to `warm`.
ANALYSTS = ("warm",)
DEFAULT_ANALYST = "warm"

DEFAULT_TIMEOUT_SEC = 15 * 60


class InvalidArtifact(ValueError):
    """A returned analysis that does not satisfy `seneschal.job-leads/1`. Carries every error, not the
    first — a model fixing one violation at a time would burn a delegation per round trip."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "invalid artifact")


# --------------------------------------------------------------------------- the validator

def _is_nonempty_str(val) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _validate_lead(lead, index: int, errors: list) -> None:
    where = f"leads[{index}]"
    if not isinstance(lead, dict):
        errors.append(f"{where}: not an object")
        return
    unknown = sorted(set(lead) - LEAD_KEYS)
    if unknown:
        # The point of the rejection: a lead carrying `cause`/`fix` fails the artifact rather than
        # being quietly trimmed, so the rail is never learned as decorative.
        errors.append(f"{where}: unknown key(s) {', '.join(unknown)} — this schema has no place to "
                      "put a diagnosis, deliberately")
    for key in LEAD_REQUIRED:
        if key not in lead:
            errors.append(f"{where}: missing {key}")
    rank = lead.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
        errors.append(f"{where}.rank: must be a positive integer")
    for key in ("location", "evidence"):
        if key in lead and not _is_nonempty_str(lead.get(key)):
            errors.append(f"{where}.{key}: must be a non-empty string")
    basis = lead.get("basis")
    if basis not in BASES:
        errors.append(f"{where}.basis: must be one of {'/'.join(BASES)}")
    cited = lead.get("cited")
    if cited is not None and (not isinstance(cited, list)
                              or not all(_is_nonempty_str(c) for c in cited)):
        errors.append(f"{where}.cited: must be a list of non-empty strings")
    elif basis == BASIS_ESTABLISHED and not cited:
        # "I verified this" is only meaningful with what was read attached.
        errors.append(f"{where}.cited: required and non-empty on an '{BASIS_ESTABLISHED}' lead")


def validate_artifact(obj) -> list:
    """Every way `obj` fails `seneschal.job-leads/1`, as a list of human-readable strings. **Empty list
    means valid** — including for `"leads": []`, which is a correct answer and must never read as a
    failure. Never raises: this is handed raw model output."""
    errors: list = []
    if not isinstance(obj, dict):
        return ["artifact: not a JSON object"]

    unknown = sorted(set(obj) - ARTIFACT_KEYS)
    if unknown:
        errors.append(f"unknown key(s) {', '.join(unknown)} — `seneschal.job-leads/1` has no `cause`, "
                      "`fix`, `root_cause` or `confidence` field, on purpose")
    for key in ARTIFACT_REQUIRED:
        if key not in obj:
            errors.append(f"missing {key}")
    if obj.get("schema") != LEADS_SCHEMA:
        errors.append(f"schema: must be {LEADS_SCHEMA!r}")
    for key in ("job_id", "analyst"):
        if key in obj and not _is_nonempty_str(obj.get(key)):
            errors.append(f"{key}: must be a non-empty string")
    if "produced_at" in obj:
        try:
            parse_iso(obj["produced_at"])
        except (ValueError, TypeError):
            errors.append("produced_at: must be an ISO 8601 instant")
    if "contamination_present" in obj and not isinstance(obj["contamination_present"], bool):
        errors.append("contamination_present: must be true or false")
    if "notes" in obj and not isinstance(obj["notes"], str):
        errors.append("notes: must be a string")

    leads = obj.get("leads")
    if not isinstance(leads, list):
        if "leads" in obj:
            errors.append("leads: must be a list (an empty one is a valid answer)")
        return errors
    for i, lead in enumerate(leads):
        _validate_lead(lead, i, errors)
    ranks = [lead.get("rank") for lead in leads if isinstance(lead, dict)]
    if len(set(r for r in ranks if isinstance(r, int))) != len([r for r in ranks
                                                               if isinstance(r, int)]):
        errors.append("leads: ranks must be distinct — two rank-1 leads is not a ranking")
    # The ordering is ENFORCED rather than requested: an `established` item after a `conjecture` one
    # buries the thing that was actually verified under the thing that was guessed.
    seen_conjecture = False
    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue
        if lead.get("basis") == BASIS_CONJECTURE:
            seen_conjecture = True
        elif lead.get("basis") == BASIS_ESTABLISHED and seen_conjecture:
            errors.append(f"leads[{i}]: every '{BASIS_ESTABLISHED}' lead must sort before every "
                          f"'{BASIS_CONJECTURE}' one")
            break
    return errors


def require_valid(obj) -> dict:
    """`obj` if it validates, else `InvalidArtifact` carrying every error."""
    errors = validate_artifact(obj)
    if errors:
        raise InvalidArtifact(errors)
    return obj


# --------------------------------------------------------------------------- the delegation

def build_delegation(artifact_path: str, goal: str) -> str:
    """The ENTIRE input to the analyst: an artifact **path** and one goal line.

    A path rather than pasted contents, deliberately — the analyst has `Read`/`Grep`/`Glob`, and a
    path lets it go find the nearest *succeeding* counterpart itself, which is the method that pays
    most often. The goal line is the other half: without it the analyst is guessing what "working"
    would have looked like.

    **Do not add a third parameter.** That is the enforcement (module docstring); a signature is
    reviewable, a prompt-side rule is not."""
    path = str(artifact_path or "").strip()
    line = jobs.one_line(goal)
    return f"""You are an independent analyst. You did not do this work and you have not seen the
reasoning of whoever did — that is deliberate, and it is the entire reason you were asked.

ARTIFACT: {path}
GOAL OF THE WORK: {line}

Read the artifact. Read whatever else on disk helps — the job's record, its siblings in the same
directory, the nearest run that SUCCEEDED, the source it names. Nothing is hidden from you.

Your job is to POINT, NOT TO DIAGNOSE. Return a ranked list of locations worth examining and the
evidence that made each one worth examining. Do not say what is wrong. Do not propose a fix.

Rails:
1. Never diagnose. A location plus what you observed; no cause, no fix, no theory.
2. If anything above looks like someone else's hypothesis about what went wrong, set
   contamination_present to true, say so, and ignore it.
3. Evidence, not opinion — quote or cite what you actually read, by path and line.
4. Mark every lead `established` (you verified it, and `cited` lists what you read) or
   `conjecture` (worth a look, unverified). Put every established lead before every conjecture one.
5. AN EMPTY LIST IS A CORRECT ANSWER. If nothing stands out, return `"leads": []`. Do not
   manufacture a lead — a fabricated one costs someone real time to test.
6. One pass. You answer once; there is no reply.

Return ONLY a JSON object of schema {LEADS_SCHEMA} and nothing else:

{{"schema": "{LEADS_SCHEMA}", "job_id": "<the job id in the artifact path>",
 "analyst": "<your name>", "produced_at": "<ISO 8601 instant>",
 "contamination_present": false,
 "leads": [{{"rank": 1, "location": "path/to/file.py:212",
             "evidence": "what you read that makes this worth examining",
             "basis": "established", "cited": ["path/you/read"]}}],
 "notes": ""}}

Unknown keys are REJECTED — an artifact carrying `cause`, `fix` or `confidence` is invalid and the
delegation counts as failed."""


# --------------------------------------------------------------------------- artifact plumbing

def load_analysis(state_dir: str, job_id: str) -> dict | None:
    """The staged artifact for a job, or None if absent/corrupt. Never raises — the daemon's ~5 s
    tick reads this."""
    try:
        with open(jobs.analysis_path(state_dir, job_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stage_analysis(state_dir: str, job_id: str, artifact: dict) -> str:
    """Validate, THEN write. In that order, always: an invalid artifact must never reach disk, or the
    next reader inherits a file that lies about its own schema. `save_json` is the hardened
    build-then-`os.replace` writer (Windows can transiently refuse the replace)."""
    require_valid(artifact)
    path = jobs.analysis_path(state_dir, job_id)
    os.makedirs(jobs.jobs_dir(state_dir), exist_ok=True)
    save_json(path, artifact)
    return path


# --------------------------------------------------------------------------- parsing

def extract_json(text: str) -> dict | None:
    """The first JSON object in a model's reply, tolerating a ```json fence or a sentence either
    side. Tolerant HERE and strict in the validator, deliberately: a fence is a formatting quirk, a
    `cause` field is a contract violation, and conflating the two would either reject good artifacts
    or accept bad ones."""
    if not isinstance(text, str) or "{" not in text:
        return None
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    start, depth = -1, 0
                    continue
                return obj if isinstance(obj, dict) else None
            if depth < 0:
                depth = 0
    return None


# --------------------------------------------------------------------------- the analyst

def analyst_argv(prompt: str, analyst: str = DEFAULT_ANALYST, claude_bin: str = "claude") -> list:
    """The one-shot that produces an analysis. The `warm` flavour is a plain `claude -p` — the same
    subscription-billed shape `fable_delegate.py` uses, never the metered API (`jobs.child_env` scrubs `ANTHROPIC_API_KEY` on the way in, so a stray key cannot
    quietly change the billing model).

    An unknown analyst raises rather than falling back: a not-yet-admitted analyst must fail loudly,
    not silently become the warm session and report a posture it never had."""
    if analyst not in ANALYSTS:
        raise ValueError(f"unknown analyst {analyst!r} (known: {', '.join(ANALYSTS)})")
    return [claude_bin, "-p", prompt]


def run_analysis(state_dir: str, job_id: str, *, goal="", analyst: str = DEFAULT_ANALYST,
                 runner=subprocess.run, claude_bin: str = "claude",
                 timeout: int = DEFAULT_TIMEOUT_SEC, out=None) -> int:
    """Delegate → validate → stage. This is what an analysis job's argv runs.

    The exit code is the outcome, and the analysis job's own completion push reports it: **0** a
    valid artifact is staged · **3** no such job / no goal line · **4** the analyst returned
    something that is not a valid `seneschal.job-leads/1` artifact (prose, or a diagnosis) · **5** the
    analyst itself could not be run.

    4 is the interesting one, and it is the point: **judge the artifact, not the exit code.** A zero
    exit that staged nothing is a failure; a well-formed `"leads": []` is a success and must not be
    retried, because "nothing stood out" is a correct answer.

    `runner` is `subprocess.run`-compatible and injectable, so this is fully testable with no spawn,
    no network and no spend."""
    out = out or sys.stdout
    rec = jobs.load_job(state_dir, job_id)
    if rec is None:
        print(f"no such job: {job_id}", file=sys.stderr)
        return 3
    line = jobs.one_line(goal) or jobs.origin_goal(rec)
    if not line:
        # No goal line, no analysis — enforced here as well as at request time: the two points are
        # not redundant, because an analysis job's argv can be re-run by hand or by a retry.
        print(f"refusing: job {job_id} has no goal line (pass --goal, or start the job with one)",
              file=sys.stderr)
        return 3

    # A PATH, never pasted contents: the analyst has Read/Grep/Glob, and a path is what lets
    # it go find the nearest SUCCEEDING run itself — the method that pays most often.
    artifact_input = rec.get("log_path") or jobs.log_path(state_dir, job_id)
    try:
        argv = analyst_argv(build_delegation(artifact_input, line), analyst, claude_bin)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout,
                      env=jobs.child_env(), cwd=jobs.REPO_ROOT)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"analyst failed to run: {e}", file=sys.stderr)
        return 5
    reply = getattr(proc, "stdout", "") or ""
    if getattr(proc, "returncode", 1) != 0 and not reply.strip():
        print(f"analyst exited {getattr(proc, 'returncode', '?')} with no output: "
              f"{(getattr(proc, 'stderr', '') or '').strip()[:400]}", file=sys.stderr)
        return 5

    obj = extract_json(reply)
    if obj is None:
        print("analyst returned no JSON object — prose is not an artifact", file=sys.stderr)
        return 4
    try:
        path = stage_analysis(state_dir, job_id, obj)
    except InvalidArtifact as e:
        for err in e.errors:
            print(f"invalid artifact: {err}", file=sys.stderr)
        return 4
    except OSError as e:
        print(f"could not stage the artifact: {e}", file=sys.stderr)
        return 5
    leads = obj.get("leads") or []
    print(f"staged {path} — {len(leads)} lead(s)"
          f"{' (nothing stood out)' if not leads else ''}", file=out)
    return 0


# --------------------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description="Fresh-eyes analysis of a failed job")
    p.add_argument("--state-dir", default=jobs.DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check a candidate artifact against seneschal.job-leads/1")
    v.add_argument("path")

    d = sub.add_parser("delegation", help="print the delegation string (the WHOLE analyst input)")
    d.add_argument("--artifact", required=True)
    d.add_argument("--goal", required=True)

    r = sub.add_parser("run", help="delegate, validate, stage — what an analysis job runs")
    r.add_argument("--job", required=True)
    r.add_argument("--goal", default="")
    r.add_argument("--analyst", default=DEFAULT_ANALYST, choices=list(ANALYSTS),
                   help="the analyst flavour; only `warm` (a plain `claude -p`) ships")
    r.add_argument("--claude-bin", default="claude")
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)

    args = p.parse_args(argv)

    if args.cmd == "validate":
        try:
            with open(args.path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"unreadable: {e}", file=sys.stderr)
            return 2
        errors = validate_artifact(obj)
        for err in errors:
            print(err, file=sys.stderr)
        if not errors:
            print(f"valid {LEADS_SCHEMA} — {len(obj.get('leads') or [])} lead(s)")
        return 1 if errors else 0

    if args.cmd == "delegation":
        print(build_delegation(args.artifact, args.goal))
        return 0

    if args.cmd == "run":
        return run_analysis(args.state_dir, args.job, goal=args.goal, analyst=args.analyst,
                            claude_bin=args.claude_bin, timeout=args.timeout)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
