#!/usr/bin/env python3
"""The truncating-write check. Stdlib only.

## What this is for

`open(path, "w")` **truncates the target before the first byte is written.** If anything then raises —
an encoding error, a disk-full, a kill — the old content is gone and the new content never arrived.
`seneschal/state/` is gitignored, so for most of its files there is no copy anywhere and the loss is
total. The classic instance: `open(state/carry-over.md, "w")`, then a `UnicodeEncodeError` under
Windows's non-UTF-8 default (or a lone surrogate from a pasted emoji) — a whole file of open loops
reduced to zero bytes, with a correct atomic helper sitting unused in the same directory.

**That last clause is the whole argument for this module.** `memory_write.py` closes the instance by
being available; nothing makes it binding. A written rule ("use the atomic helper") cannot refuse
anything. A check that fails the build can.

## What it flags, and what it deliberately does not

A finding is a **truncating write whose target expression resolves to a `state/` path**:

* `open(p, "w")` / `"wb"` / `"w+"`, and the same through `io.open` / `codecs.open` / `Path.open`
* `Path(p).write_text(...)` / `.write_bytes(...)`
* a `mode=` that is not a literal, because it cannot be shown to be safe

Four exclusions, each of which would otherwise make this a wall rather than a check:

1. **`os.open` is not `open`.** It takes integer flags, and the shape this tree uses it for is
   `os.open(p, O_CREAT|O_EXCL|O_WRONLY)` — the sidecar lock in `memory_write._FileLock` and
   `reminders_acks.queue_lock`. `O_EXCL` **fails** when the target exists; it is the opposite of a
   truncating write.
2. **A write to a staging file is the correct pattern, not a violation.** If the enclosing function
   hands the same name to `os.replace`/`os.rename` as its source, `open(tmp, "w")` is exactly what
   this module is asking people to do. Without this rule most findings are the very pattern the check
   exists to promote, and flagging correct code as the violation is how a gate gets deleted.
3. **Append mode does not truncate.** `open(p, "a")` is the right shape for the append-only ledgers
   (`metrics.jsonl` and friends) and is left alone. Its *own* failure mode is a lost concurrent entry,
   which is `memory_write.append_text`'s problem and not this one's.
4. **`test_*.py` is not scanned.** A test writing `open(tmpdir/"x", "w")` is not a state write. The
   honest cost is stated rather than hidden: this means the check cannot see a test that writes to a
   *live* state dir — a real hazard, but a **different** bug with a different fix, and folding it in
   here would drown the signal in tmpdir noise.

## The limit that matters, stated plainly

**The losses this guards against usually come from code this check cannot see** — an ad-hoc script
composed in a turn and never committed. This module gates *tracked Python*, so what it actually buys
is that the tree's own machinery cannot regress and that the rule is written somewhere that runs.
**It is not a claim that `state/` is safe from a turn.** The half that addresses prompt-side writers is
`memory_write.py`'s CLI (so a skill has something to call other than an improvisation) and the backup
rotation in `state_backup.py` (so a total loss costs a day instead of everything).

**And there is a layer this one structurally cannot be.** Everything above is about a write that goes
*around* the helper. A write can also go *through* it — the CLI called correctly but handed an
**empty stdin** by a staging step that already failed, atomically replacing a full file with zero
bytes and exiting 0. So `memory_write.write_text` itself refuses the destructive case
(`EmptyWriteRefused`; `--allow-empty` to mean it). None of these layers is sufficient alone.

## Allowlist

`seneschal/state-write-allowlist.json`, `reason` required per entry, matched on `path` + `symbol`
rather than a line number so it survives an edit above it. An entry is **debt made visible**, not an
exemption granted quietly — the file is short on purpose and each row says why it is there.

Usage:
  python check_state_writes.py             # report, exit 0
  python check_state_writes.py --enforce   # exit 1 on any finding (this is what CI runs)
  python check_state_writes.py --audit     # both counts + the staged sites, for a PR body
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gate_git  # noqa: E402  — the working-tree enumeration every local gate shares

ALLOWLIST_FILE = os.path.join("seneschal", "state-write-allowlist.json")

#: A path segment literally named `state` — `"../state"`, `"seneschal/state/x.json"`, `"state"`.
STATE_SEG = re.compile(r"(^|[/\\])state([/\\]|$)")

#: An identifier that names a state path. Deliberately narrow: `state_dir` and friends, never a bare
#: `path`, which is every second variable in this tree and would taint everything.
STATE_IDENT = re.compile(r"state_dir|statedir|state_path|state_file|^state$|_state$", re.IGNORECASE)

#: Calls that pass a path through unchanged, so taint survives them.
PATHY = {"join", "normpath", "abspath", "expanduser", "realpath", "Path", "str", "fspath", "resolve"}

TRUNCATING_ATTRS = {"write_text", "write_bytes"}

#: Modes that truncate. `x` is exclusive-create — it *fails* on an existing file, so it cannot
#: destroy one. `a` appends. `r+` can corrupt but does not truncate, and is not used in this tree.
def _truncates(mode: str) -> bool:
    return mode.startswith("w") or mode == "<dynamic>"


class Finding:
    __slots__ = ("path", "line", "kind", "symbol", "source")

    def __init__(self, path, line, kind, symbol, source):
        self.path, self.line, self.kind = path, line, kind
        self.symbol, self.source = symbol, source

    def key(self):
        return (self.path.replace("\\", "/"), self.symbol)

    def __str__(self):
        where = f"{self.path}:{self.line}"
        sym = self.symbol or "<module>"
        return f"{where}  [{self.kind}] in {sym}(): {self.source}"


class _Taint:
    """A per-module taint pass: which expressions name a path under a `state/` directory.

    Intra-module and interprocedural in one direction only — a call site with a tainted argument
    taints the callee's *parameter*, which is what makes `_save_json(os.path.join(state_dir, f), d)`
    reach the `open()` inside `_save_json`. Iterated to a fixpoint (capped), because taint discovered
    in one pass creates call sites that taint in the next.

    Two rules exist purely for **recall**, each for a miss a per-module pass cannot see:

    * **Cross-module constants** (`exports`). An archon's path module (`<id>_paths.py`) declares
      `QUEUE_FILE = STATE_DIR / "workup-queue.json"` and a sibling tool writes it through
      `<id>_paths.QUEUE_FILE` / a `from … import`. Nothing in the writer's own text says "state",
      so a strictly per-module pass reports a genuine in-place write clean.
    * **argparse defaults.** `p.add_argument("--offset-file", default=DEFAULT_OFFSET_FILE)` moves a
      tainted constant into `args.offset_file`, an attribute whose *name* says nothing. This is how
      `discord_poll.write_offset` and `rag_projects`'s `projects-map.md` write were being missed."""

    def __init__(self, tree: ast.Module, exports: dict | None = None):
        self.tree = tree
        self.exports = exports or {}
        self.names: set[str] = set()
        self.arg_dests: set[str] = set()
        self.params: set[tuple[str, str]] = set()
        self._seed_imports()
        self.funcs = {n.name: n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self._enclosing = {}
        for fd in self.funcs.values():
            for child in ast.walk(fd):
                self._enclosing[id(child)] = fd.name
        self._solve()

    def _seed_imports(self) -> None:
        """`from x_paths import QUEUE_FILE` → taint the local name; remember module aliases so
        `x_paths.QUEUE_FILE` resolves too."""
        self.module_aliases: dict[str, str] = {}
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mod = n.module.rsplit(".", 1)[-1]
                exported = self.exports.get(mod, set())
                for a in n.names:
                    if a.name in exported:
                        self.names.add(a.asname or a.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    mod = a.name.rsplit(".", 1)[-1]
                    if mod in self.exports:
                        self.module_aliases[a.asname or mod] = mod

    def enclosing(self, node) -> str:
        return self._enclosing.get(id(node), "")

    def params_of(self, fn: str) -> set[str]:
        return {a for (f, a) in self.params if f == fn}

    def tainted(self, e, scope: set[str] = frozenset()) -> bool:
        if e is None:
            return False
        if isinstance(e, ast.Constant):
            return isinstance(e.value, str) and bool(STATE_SEG.search(e.value))
        if isinstance(e, ast.Name):
            return e.id in self.names or e.id in scope or bool(STATE_IDENT.search(e.id))
        if isinstance(e, ast.Attribute):
            if isinstance(e.value, ast.Name):
                mod = self.module_aliases.get(e.value.id)
                if mod and e.attr in self.exports.get(mod, set()):
                    return True                       # `x_paths.QUEUE_FILE`
                if e.value.id == "args" or e.attr in self.arg_dests:
                    if e.attr in self.arg_dests:
                        return True                   # `args.offset_file`, via an argparse default
            return bool(STATE_IDENT.search(e.attr)) or self.tainted(e.value, scope)
        if isinstance(e, ast.Subscript):
            return self.tainted(e.value, scope)
        if isinstance(e, ast.BinOp):          # pathlib's `/`, and str concatenation
            return self.tainted(e.left, scope) or self.tainted(e.right, scope)
        if isinstance(e, ast.BoolOp):         # `p = arg or DEFAULT_STATE_DIR`
            return any(self.tainted(v, scope) for v in e.values)
        if isinstance(e, ast.IfExp):
            return self.tainted(e.body, scope) or self.tainted(e.orelse, scope)
        if isinstance(e, ast.JoinedStr):
            for v in e.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str) \
                        and STATE_SEG.search(v.value):
                    return True
                if isinstance(v, ast.FormattedValue) and self.tainted(v.value, scope):
                    return True
            return False
        if isinstance(e, ast.Call):
            name = _callee(e)
            if name in PATHY:
                return any(self.tainted(a, scope) for a in e.args)
            if name and STATE_IDENT.search(name):
                return True
            # An in-module helper handed a tainted argument returns a state path often enough that
            # missing it costs more than the odd false positive: `_path(state_dir)` is the shape.
            if name in self.funcs and any(self.tainted(a, scope) for a in e.args):
                return True
        return False

    def _solve(self) -> None:
        for _ in range(8):
            before = (len(self.names), len(self.params))
            for n in ast.walk(self.tree):
                if isinstance(n, ast.Assign):
                    if self.tainted(n.value, self.params_of(self.enclosing(n))):
                        for t in n.targets:
                            if isinstance(t, ast.Name):
                                self.names.add(t.id)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    if self.tainted(n.value, self.params_of(self.enclosing(n))):
                        self.names.add(n.target.id)
                elif isinstance(n, ast.Call):
                    if _callee(n) == "add_argument":
                        dest = _argparse_dest(n)
                        if dest and any(kw.arg == "default"
                                        and self.tainted(kw.value,
                                                         self.params_of(self.enclosing(n)))
                                        for kw in n.keywords):
                            self.arg_dests.add(dest)
                    fd = self.funcs.get(_callee(n))
                    if not fd:
                        continue
                    scope = self.params_of(self.enclosing(n))
                    positional = [a.arg for a in fd.args.args]
                    for i, arg in enumerate(n.args):
                        if i < len(positional) and self.tainted(arg, scope):
                            self.params.add((fd.name, positional[i]))
                    for kw in n.keywords:
                        if kw.arg and self.tainted(kw.value, scope):
                            self.params.add((fd.name, kw.arg))
            for fd in self.funcs.values():                     # `def f(state_dir=DEFAULT_STATE_DIR)`
                names = [a.arg for a in fd.args.args]
                defaults = list(fd.args.defaults)
                for a, d in zip(names[len(names) - len(defaults):], defaults):
                    if self.tainted(d):
                        self.params.add((fd.name, a))
            if (len(self.names), len(self.params)) == before:
                break


def _argparse_dest(call: ast.Call) -> str:
    """The attribute `add_argument(...)` will hang off the namespace: explicit `dest=`, else the
    first long option with dashes turned into underscores, else a positional name."""
    for kw in call.keywords:
        if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            if a.value.startswith("--"):
                return a.value[2:].replace("-", "_")
            if not a.value.startswith("-"):
                return a.value.replace("-", "_")
    return ""


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


#: Modules whose `.open` is the *builtin* signature `open(file, mode)` rather than pathlib's bound
#: `Path.open(mode)`. The two put the mode in different argument positions, and reading `Path(p)
#: .open("w")` with the builtin offsets takes `"w"` for the filename and `"r"` for the mode — which
#: is how `rag_projects`'s write of `state/projects.jsonl` sat unreported.
_OPEN_MODULES = {"io", "codecs"}


def _open_form(call: ast.Call) -> str | None:
    """`"builtin"` for `open(f, m)` / `io.open(f, m)`, `"bound"` for `p.open(m)`, else `None`."""
    f = call.func
    if isinstance(f, ast.Name):
        return "builtin" if f.id == "open" else None
    if isinstance(f, ast.Attribute) and f.attr == "open":
        if isinstance(f.value, ast.Name):
            if f.value.id == "os":
                return None            # exclusion 1: int flags, not a mode string
            if f.value.id in _OPEN_MODULES:
                return "builtin"
        return "bound"
    return None


def _mode_of(call: ast.Call, form: str) -> str:
    idx = 1 if form == "builtin" else 0
    if len(call.args) > idx:
        a = call.args[idx]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
        return "<dynamic>"
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else "<dynamic>"
    return "r"


def _staged_names(tree: ast.Module, funcs) -> dict:
    """Per enclosing function, the local names handed to `os.replace`/`os.rename` as the SOURCE.

    A truncating write to one of those is the build-then-replace pattern — exclusion 2."""
    out = {}
    for scope in list(funcs.values()) + [tree]:
        acc = out.setdefault(getattr(scope, "name", ""), set())
        for c in ast.walk(scope):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                    and c.func.attr in {"replace", "rename"} \
                    and isinstance(c.func.value, ast.Name) and c.func.value.id == "os" \
                    and c.args and isinstance(c.args[0], ast.Name):
                acc.add(c.args[0].id)
    return out


def _target_of(node: ast.Call, form: str | None):
    if form is None:                             # `.write_text` / `.write_bytes` — the receiver
        return node.func.value
    if form == "bound":                          # `Path(...).open("w")` — also the receiver
        return node.func.value
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg in ("file", "filename"):
            return kw.value
    return None


def tracked_python(root: str) -> list[str]:
    """Every `*.py` in the WORKING TREE — `git ls-files` plus untracked-not-ignored files
    (`gate_git.working_tree_paths`), so a not-yet-added script is scanned locally before it is
    committed rather than first on CI. A clean checkout reads identically."""
    return sorted(gate_git.working_tree_paths(root, "*.py"))


def build_exports(sources: dict) -> dict:
    """`{module_basename: {NAMES that hold a state path}}`, for the cross-module rule.

    Keyed on the basename because this tree imports siblings flatly (`import x_paths`), and a
    dotted package path would resolve to nothing. A collision between two same-named modules in
    different trees can only *add* taint, i.e. cost precision, never recall — and there is no such
    collision today."""
    exports = {}
    for rel, src in sources.items():
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue
        local = _Taint(tree)
        names = set()
        for n in tree.body:                    # top level only: a module constant, not a local
            if isinstance(n, ast.Assign) and local.tainted(n.value):
                names.update(t.id for t in n.targets if isinstance(t, ast.Name))
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                    and local.tainted(n.value):
                names.add(n.target.id)
        if names:
            exports[os.path.splitext(os.path.basename(rel))[0]] = names
    return exports


def scan_source(src: str, rel: str, exports: dict | None = None) -> tuple[list[Finding], list[Finding]]:
    """Return `(in_place, staged)` for one module's source. Pure — the tests drive this directly."""
    tree = ast.parse(src, filename=rel)
    taint = _Taint(tree, exports)
    staged_by_fn = _staged_names(tree, taint.funcs)
    lines = src.replace("\r\n", "\n").split("\n")
    in_place, staged = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        form = _open_form(node)
        if form:
            mode = _mode_of(node, form)
            if not _truncates(mode):
                continue
            kind = f"open({mode})"
        elif isinstance(node.func, ast.Attribute) and node.func.attr in TRUNCATING_ATTRS:
            form, kind = None, node.func.attr
        else:
            continue
        target = _target_of(node, form)
        fn = taint.enclosing(node)
        src_line = lines[node.lineno - 1].strip()[:120] if node.lineno <= len(lines) else ""
        found = Finding(rel, node.lineno, kind, fn, src_line)
        if isinstance(target, ast.Name) and target.id in staged_by_fn.get(fn, ()):
            if taint.tainted(target, taint.params_of(fn)):
                staged.append(found)
            continue
        if taint.tainted(target, taint.params_of(fn)):
            in_place.append(found)
    return in_place, staged


def scan(root: str) -> tuple[list[Finding], list[Finding]]:
    sources = {}
    for rel in tracked_python(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                sources[rel] = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
    # Every tracked module contributes constants, including the test files that are not themselves
    # scanned: a path constant is a fact about the tree regardless of who declared it.
    exports = build_exports(sources)
    in_place, staged = [], []
    for rel, src in sources.items():
        if os.path.basename(rel).startswith("test_"):          # exclusion 4
            continue
        try:
            a, b = scan_source(src, rel, exports)
        except SyntaxError:
            continue                          # `python-syntax` in CI owns this failure, not us
        in_place.extend(a)
        staged.extend(b)
    return in_place, staged


def _function_of(tree: ast.Module) -> dict:
    """node id -> nearest enclosing function name, or absent for module level. The same walk
    `_Taint.__init__` builds for `self._enclosing`, factored out standalone so a caller that only
    wants this (not a full taint pass) doesn't pay for one."""
    out = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for child in ast.walk(fn):
            out[id(child)] = fn.name
    return out


def scan_source_for_replace(src: str, rel: str) -> list[Finding]:
    """Every `os.replace(` call in one module's source. Pure — tests drive this directly.

    Part of the migration tracker below, not the truncating-write scan above: it is never
    consulted for `--enforce`."""
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError:
        return []
    fn_of = _function_of(tree)
    lines = src.replace("\r\n", "\n").split("\n")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "replace" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "os":
            src_line = lines[node.lineno - 1].strip()[:120] if node.lineno <= len(lines) else ""
            found.append(Finding(rel, node.lineno, "os.replace", fn_of.get(id(node), ""), src_line))
    return found


def scan_replace_sites(root: str) -> list[Finding]:
    """`os.replace(` call sites in tracked, non-test Python outside `stateio.py` itself.

    **Report-only migration tracking, not a truncating-write finding.**
    `stateio.write_text_atomic` / `write_json_atomic` own the one hardened, Windows-
    `PermissionError`-retrying `os.replace` this tree needs, and every other `os.replace(` site is
    one the migration hasn't reached yet. This finds them so a later change can watch the count
    go to zero. **It never affects `main`'s exit code** — pinning `--enforce` to zero here would
    fail every PR until every migration lands, which is exactly the "gate red on the day it
    merges gets disabled" failure every other check in this tree avoided by shipping report-only
    first."""
    findings: list[Finding] = []
    for rel in tracked_python(root):
        base = os.path.basename(rel.replace("\\", "/"))
        if base == "stateio.py" or base.startswith("test_"):
            continue
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_source_for_replace(src, rel))
    return findings


def load_allowlist(root: str) -> dict:
    """`{(path, symbol): reason}`. A missing file is an empty allowlist, not an error — the check
    must work in a checkout that has not got one yet."""
    try:
        with open(os.path.join(root, ALLOWLIST_FILE), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    out = {}
    for entry in raw.get("allow", []):
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"{ALLOWLIST_FILE}: entry {entry!r} has no `reason`. "
                             "Debt is allowed here; unexplained debt is not.")
        out[(entry["path"].replace("\\", "/"), entry.get("symbol") or "")] = reason
    return out


def repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".."))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Truncating writes to state/ paths.")
    p.add_argument("--root", default=repo_root())
    p.add_argument("--enforce", action="store_true", help="exit 1 on any un-allowlisted finding")
    p.add_argument("--audit", action="store_true", help="also print the correct staged writes")
    args = p.parse_args(argv)

    in_place, staged = scan(args.root)
    allow = load_allowlist(args.root)

    live = [f for f in in_place if f.key() not in allow]
    excused = [f for f in in_place if f.key() in allow]

    print(f"state/ write audit over tracked Python (root: {args.root})")
    print(f"  write-temp-then-os.replace (correct) : {len(staged)}")
    print(f"  in-place truncating writes           : {len(in_place)}"
          f"   [{len(excused)} allowlisted, {len(live)} live]")

    if args.audit and staged:
        print("\n-- correct (staged) writes --")
        for f in sorted(staged, key=lambda f: (f.path, f.line)):
            print(f"  {f}")
    if excused:
        print("\n-- allowlisted in-place writes (visible debt) --")
        for f in sorted(excused, key=lambda f: (f.path, f.line)):
            print(f"  {f}\n      reason: {allow[f.key()]}")
    if live:
        print("\n-- IN-PLACE WRITES TO state/ --")
        for f in sorted(live, key=lambda f: (f.path, f.line)):
            print(f"  {f}")
        print("\n  Each of these truncates its target before writing a byte. Use "
              "`memory_write.write_text`\n  (text) or the module's own build-then-`os.replace` "
              "writer, or add a row with a `reason`\n  to " + ALLOWLIST_FILE + ". "
              "Why this is a blocking check: this module's docstring.")
    if live and not args.enforce:
        print(f"\n  [report-only] {len(live)} finding(s); pass --enforce to fail on them.")

    # The stateio migration tracker — REPORT-ONLY,
    # always, on every invocation including --enforce: this count is never part of the exit code.
    replace_sites = scan_replace_sites(args.root)
    print(f"\nos.replace( outside stateio.py, tracked non-test Python (report-only, stateio "
          f"migration tracker): {len(replace_sites)}")
    if args.audit and replace_sites:
        print("\n-- os.replace( sites outside stateio.py --")
        for f in sorted(replace_sites, key=lambda f: (f.path, f.line)):
            print(f"  {f}")

    return 1 if (args.enforce and live) else 0


if __name__ == "__main__":
    sys.exit(main())
