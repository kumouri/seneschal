"""The Jobs panel's one write: the VALUES of `jobs.py`'s two concurrency caps, the owner's to set
(`seneschal/docs/background-jobs-spec.md` §3.12).

**The SAME writer as the CLI, not a copy of it.** Unlike every other reader in this backend, this does
not duplicate the daemon's logic (cockpit-spec ruling 3's usual answer): it loads
`seneschal/scripts/job_caps.py` itself and calls its `save()`, so the validation bounds, the atomic write
and the file shape cannot drift between `jobs.py caps set` and this panel. That is safe for the reason
`doc_status.py` gives for its own import — the cockpit is supervised from, and rebuilt on every HEAD
move of, the same checkout the daemon runs, so there is no commit skew between writer and reader to
tolerate. It is loaded **by file path** (importlib), never by putting `seneschal/scripts/` on `sys.path`:
that directory's generically-named modules (`jobs`, `governor`, …) would shadow this package's own when
the tests run under `unittest discover -s cockpit/server`.

**The in-flight count comes from `jobs.py caps --json`, in a subprocess.** The number on the panel
must be the number a `start` is judged against — the liveness-checked, agent-only `concurrency()` —
and re-deriving that here would be the third copy ruling 3 forbids. `jobs.py` cannot be imported
in-process for the shadowing reason above, so it runs as a child (≈ a few hundred ms, cached for
`COUNT_TTL_SEC`), with no console window on Windows. Any failure degrades to `in_flight: null` and a
`count_error` — never a 500, and never a guessed number.

**Lowering a cap below the live count cancels nothing.** The panel says so; `save()` only changes what
the next `start` is refused at.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "seneschal" / "scripts"
JOBS_SCRIPT = SCRIPTS_DIR / "jobs.py"

COUNT_TTL_SEC = 10.0
COUNT_TIMEOUT_SEC = 20.0
_CREATE_NO_WINDOW = 0x08000000

_count_cache: dict = {}


class CapsUnavailable(RuntimeError):
    """`seneschal/scripts/job_caps.py` could not be loaded from this deployment."""


def _job_caps():
    """`seneschal/scripts/job_caps.py`, loaded by path and cached in `sys.modules` under a private name."""
    name = "seneschal_scripts_job_caps"
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPTS_DIR / "job_caps.py"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — a panel may never be the thing that 500s the cockpit
        raise CapsUnavailable(f"{path} is not loadable: {exc}") from exc
    sys.modules[name] = module
    return module


def _default_counter(state_dir: Path) -> dict:
    """Run `jobs.py caps --json` against `state_dir` and return its parsed view."""
    kwargs: dict = {"capture_output": True, "text": True, "timeout": COUNT_TIMEOUT_SEC,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    res = subprocess.run([sys.executable, str(JOBS_SCRIPT), "--state-dir", str(state_dir),
                          "caps", "--json", "--session", ""], **kwargs)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or f"exit {res.returncode}").strip()[-300:])
    return json.loads(res.stdout)


#: Swappable for tests — `(state_dir) -> jobs.py caps --json`'s view.
counter = _default_counter


def _live_counts(state_dir: Path, now: Optional[float] = None) -> dict:
    now = time.monotonic() if now is None else now
    key = str(state_dir)
    hit = _count_cache.get(key)
    if hit and now - hit[0] < COUNT_TTL_SEC:
        return hit[1]
    try:
        view = counter(state_dir)
        out = {"in_flight": int(view["global"]), "uncounted": int(view.get("uncounted") or 0),
               "count_error": None}
    except Exception as exc:  # noqa: BLE001 — degrade to "unknown", never a guess
        out = {"in_flight": None, "uncounted": None, "count_error": str(exc)[:300] or "failed"}
    _count_cache[key] = (now, out)
    return out


def clear_cache() -> None:
    _count_cache.clear()


def read_caps(state_dir: Any) -> dict:
    """The panel's payload: the caps in force, their bounds, and the live agent-job count."""
    state_dir = Path(state_dir)
    try:
        jc = _job_caps()
    except CapsUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    caps = jc.load(str(state_dir))  # a bad file warns once to this backend's own log, and says so here
    return {"available": True,
            "soft_per_session": caps["soft_per_session"], "hard_global": caps["hard_global"],
            "source": caps["source"], "updated_at": caps.get("updated_at"),
            "updated_by": caps.get("updated_by"), "error": caps.get("error"),
            "limits": jc.limits(), **_live_counts(state_dir)}


def save_caps(state_dir: Any, soft: Any, hard: Any) -> dict:
    """Validate + write through `job_caps.save(updated_by="cockpit")`. Raises `ValueError` (the
    module's `CapsError`) on a bad pair — nothing is written — and `CapsUnavailable` if the writer
    cannot be loaded. Returns the fresh `read_caps` payload."""
    state_dir = Path(state_dir)
    jc = _job_caps()
    jc.save(str(state_dir), soft, hard, updated_by="cockpit")
    _count_cache.pop(str(state_dir), None)
    return read_caps(state_dir)
