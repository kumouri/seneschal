# Subdirectories for `seneschal/scripts/` — why the directory stays flat

**Status:** `SPEC-ONLY(the axis is CLOSED by its own measurements — not backlog)` — **nothing has
moved and nothing should.** No `git mv`, no import rewrite, no `.claude/rules/` file. This document
records a design question that was worked to a conclusion so it is not re-opened from scratch: *should
`seneschal/scripts/` grow per-subject subdirectories (or path-scoped rule files) so that each subject's
grounding loads only when that subject is touched?* **No, on both instruments.** §5 is the arithmetic
that closes it; §3 and §4 are the mechanics any future attempt must not re-learn.

**Parent:** `context-budget-spec.md` (the byte ratchet this question arose from). **Sibling:**
`grounding-restructure-spec.md` — the same split one level up, which *did* pay (root router,
sub-routers, the mode router).

---

## 1. What the prize would have been, stated correctly

**It is not bytes.** Moving a cluster's grounding into its own file does not reduce the routed-tree
total: every byte still exists and is still reachable. Anyone selling the change on a byte saving has
mis-sold it — the per-file number falls and the total does not move.

**The prize is proportionality.** A router that covers the whole scripts directory loads whether the
work is a comms bridge or a RAG indexer. A per-subject file would load only when that subject is
touched, so context cost becomes proportional to what is being worked on. The second-order hope was
that each subject's invariants would argue against their own budget instead of a shared pool, where
the rational move for each individual author is to spend a little more.

That is the whole case. Everything below tests whether it survives contact.

---

## 2. The shape of the directory

The scripts directory is not a mesh. Measured by **non-test** importers, it is a small **hub set** —
`sentinel.py`, `reminders_acks.py`, `governor.py`, the jobs/outbox/presence common modules — plus a
long tail of leaves with zero non-test importers. That shape is what makes an incremental move
*possible* at all, and it also defines the **spine**: the modules imported across many subjects.
**The spine is defined by measured fan-in, not by taste.**

Two subjects look like clusters and are not:

- **reminders is not a cluster, it is the spine.** `sentinel.py`, `reminders_*.py` and `ack.py` have
  many inbound and outbound edges spanning jobs, comms, sessions and the daemon's tick. There is no
  boundary to draw that does not cut a load-bearing edge. It stays flat, and that is a result rather
  than a deferral.
- **comms carries one edge that must never be filed under comms**: `telegram_send.py →
  reminders_acks.py`, the send-time ack gate (a second sender the reminder queue's fire-time gate
  never covered). Filing `telegram_send.py` under a `comms/` subject puts the gate's invariant where a
  turn working on reminders never looks. **`telegram_send.py` stays on the spine whatever instrument
  is used.**

And one near-miss worth remembering: `telegram_ingest.py` shares a prefix with the live Telegram
bridge modules but is an **export reader** for the message archiver, sharing no code with them. **A
subject axis read off filenames would have got it wrong.**

---

## 3. The directory instrument — the import model

### 3.1 What breaks

Scripts are invoked as `python seneschal/scripts/x.py`, which puts **that file's directory** at
`sys.path[0]`, and they import each other flat. Some drive a sibling's `main(argv=[...])` in-process;
some spawn a sibling as a subprocess via `os.path.join(SCRIPT_DIR, "<name>.py")`.

Moving a file breaks flat sibling imports **only across a new directory boundary**. Inside a moved
self-contained cluster, flat imports keep working because `sys.path[0]` follows the file.

### 3.2 Four models, probed rather than reasoned about

Each built as a throwaway tree (a spine module at the top, a leaf in a subdirectory importing it, a
test beside the leaf) and run against the two invocation shapes that must survive:

| Model | Direct `python <path>` | `unittest discover -s <root> -p "test_*.py"` |
|---|---|---|
| **M1** plain subdir, no `__init__.py` | ❌ `ModuleNotFoundError` across the boundary | ❌ **silently finds nothing** |
| **M2** subdir + `__init__.py` | ❌ across the boundary only | ✅ |
| **M3** `__init__.py` + a two-line parent-path shim | ✅ | ✅ |
| **M4** true packages, relative imports, `python -m` | ✅ via `-m` | ❌ `_FailedTest` on every module |

**M1 is a trap, not merely wrong.** Discovery recurses into a subdirectory only if it is an importable
package (namespace-package discovery is gone). In a real tree the run stays **green**, because the
top-level suites still run — the moved cluster's tests simply cease to exist. **The `__init__.py` is
the difference between the tests running and silently not running, with a green tick either way.**
A test counter built on the same `loader.discover` sees exactly what the runner sees, so it detects the
loss but (being report-only by design) cannot block it — which is why an empty `__init__.py` would be a
precondition of any move, and every move's PR would state an expected per-suite test delta of **±0**.

**M4 is the theoretically correct answer and loses on call sites.** `python -m seneschal.scripts.<sub>.x`
would have to replace `python x.py` in every mode file and SKILL the assistant follows literally, every
`*_SETUP.md`, the scheduled-task wrappers, every `SCRIPT_DIR` spawn site, the jobs spawn preflight —
and in user-level, unversioned command files no PR can change. Every miss fails at runtime, not at
merge, and it breaks discovery as CI runs it today.

### 3.3 Had it been built: a flat spine, packaged leaves, a shim only where earned

> Leaves move into subdirectories that are real packages (`__init__.py`, always). The spine stays flat.
> A leaf that imports the spine adds the parent-path shim; a leaf that imports nothing outside its own
> cluster adds nothing.

It matches the idiom the directory already uses (`sys.path.insert(0, SCRIPT_DIR)`), fails loudly at
import time when a shim is missing, and is incrementally adoptable. Its residual cost: a module could
be imported both as `x` and as `<sub>.x` — two module objects, two sets of module-level state — so a
cluster's modules must be imported one way only. And **cluster-local generic names (`common.py`,
`utils.py`) are forbidden**; keep `<subject>_common.py` even when the prefix looks redundant, because
two leaves each inserting their own directory at `sys.path[0]` would otherwise collide.

### 3.4 The move discipline

Recorded because it is the right discipline for *any* multi-file relocation in this repo:

- **Sweep before and after.** A literal-basename grep is complete for Python here only because no
  script path is built at runtime from parts — every spawn site joins a literal basename onto
  `SCRIPT_DIR`. Verify that with `ast` before trusting a grep. The residual risk is prose (mode files,
  SKILLs, setup guides) and scheduled tasks.
- **Classify every hit:** moves with the cluster / must be edited in the same PR / **must NOT be
  edited — dated evidence.** A spec or ledger entry recording a measurement taken at a path on a date is
  a statement about the past, not a pointer; rewriting it falsifies the record to satisfy a grep. A
  sweep is **reviewed, never applied mechanically**.
- **One wave, one PR**, so one revert restores every path atomically. A half-applied move is a tree
  where some references point at the old path and some at the new, and no single revert fixes it.
- **Moves are verbatim.** A rewrite and a move in one commit cannot be reviewed.
- **A wave is not landed until one command from its own setup doc has run from its new path**, after
  the daemon's graceful reload on merge — not a forced restart, which discards the evidence the
  graceful path worked.

---

## 4. The rules instrument — `.claude/rules/` with `paths:`

A rule file with `paths:` frontmatter loads on the same on-demand trigger as a nested `CLAUDE.md`, keyed
on a glob instead of a directory — which would buy §1's proportionality with **no `.py` moves at all**,
and so without §3's `__init__.py` trap, collision hazard or reference sweep.

### 4.1 What was measured

Probed in throwaway trees with random 25-character sentinel strings (appearing in no prompt, so the
instrument can under-report but never over-report) and the `InstructionsLoaded` hook as harness-side
ground truth, one variable per headless `claude -p` run, a **negative-control rule** identical except
for its glob:

| Trigger | `paths:` rule | nested `CLAUDE.md` |
|---|---|---|
| `Read` a matching / in-directory file | ✅ | ✅ |
| Bash `cat` of that file | ❌ | ❌ |
| `Grep` / `Glob` across the directory | ❌ | ❌ |
| **`Write` a new file there** (verified created) | ❌ | ❌ |
| session cwd inside the directory, no tools | ❌ | ✅ (ancestor walk at launch) |

Further findings:

- **The trigger is the glob the *read* file matches**, not "a read happened"; a read matching no glob
  loads nothing. A rule with no `paths:` loads at launch.
- **Once per session.** Two matching reads produced one injection — a rule does not re-inject per tool
  call.
- **Obeyed as instruction**, not treated as optional reference, and the obedience tracked the glob
  exactly in both directions.
- **Compaction:** the documentation says neither nested files nor path-scoped rules are re-injected
  after compaction; the session transcripts showed the harness **does** re-attach the matched set.
  (The hook logged no `compact` events at all — it is authoritative for `path_glob_match` and is not a
  complete census.)
- **Project rules load headless** without `--setting-sources`, and `paths:` resolves against the
  **project root**, not the rule file's directory.

### 4.2 The `Write` row indicts both instruments

A turn that **creates a new script** in a cluster receives none of the cluster's invariants — under a
rule file *and* under a subdirectory router. That is the "a rule that only exists one hop away is a rule
that does not run" failure at the moment it is most expensive. So, whichever instrument: **every
imperative and every refusal stays in the always-reached parent; only evidence may move out.**

### 4.3 Glob hazards

- **Prefix globs over-match.** `telegram_*` would have swallowed `telegram_send.py` and with it the
  ack-gate invariant (§2); `archive_*` must not reach unrelated `*_archive` modules. Verified negatives
  are the whole value of a glob test.
- **Enumerated globs rot.** Avoiding the prefix means enumerating filenames — a hand-maintained list
  with nothing checking it. The first draft of one named a test module that did not exist.
- **The coupling reaches the glob anyway.** The most-coupled cluster pays the most frontmatter,
  precisely because it cannot use a prefix. "A glob needs no boundary" is true of the import boundary
  only.
- **A glob-keyed rule fires for the model and is invisible to a human browsing the directory.** It must
  be pointed at from the parent exactly as a sub-router would be.

---

## 5. Why the axis closed

### 5.1 The prize was already collected

The trim rule the scripts grounding already lives under is: *the rule stays, the evidence goes one
hop out — to the module docstring, the setup guide, or the spec named beside it.* Classified clause by
clause, a subject's grounding is mostly **rule**, which cannot move (§4.2), and a small **evidence**
remainder. Checked clause by clause, **every evidence clause already had a fuller destination one hop
out** — setup guides and module docstrings several times the entry's size, which load only when read
and never enter startup context. **Orphaned evidence: zero.** The proportionality §1 promised had been
collected by ordinary docstrings and setup guides, without a rule file, without a `git mv`, and without
anybody calling it a wave.

### 5.2 The arithmetic is general

A new artifact carries an irreducible **floor**: frontmatter (for a rule file) plus a point-back
preamble that stops it being mistaken for the authority on the rules, plus the parent's pointer to it.
Move X bytes out: the parent falls by X and the new file rises by `floor + X`.

> **Net routed-tree bytes = floor + pointer, whatever X is.** X cancels. No cluster, of any size, can
> pass a "the parent must shrink by more than the new files grow" criterion by *relocating* — on the
> rules axis or the directory axis. The only way to pass is to **delete** prose, and deleting needs no
> new artifact.

An earlier draft proposed a break-even threshold ("a cluster needs more than ~floor bytes of orphaned
evidence to pay"). **There is no such threshold.** Orphaned evidence changes whether a file is
*honest*; it never changes the net. And with zero orphaned evidence, anything moved is not relocation
but a **third representation** of a rule (behind the docstring and the parent) — one rule, several
copies, nothing comparing them — which is independently disqualifying.

### 5.3 The spine has no readership boundary

A path-scoped rule reaches exactly the turns that read the cluster's own files. For a spine cluster,
the turns that most need its invariants are **importers outside it**: `presence.py` owns the tick that
calls the reminder check and reading it loads no reminders rule; `activity_day.py`, imported by both
`ack.py` and `reminders_dequeue.py`, carries the after-midnight rule and gets nothing either. Test files
straddle clusters (a reactions test importing comms, reminders and the daemon at once), and a rule fires
per file, so each must be assigned to one cluster and be wrong for the other. **For the spine, a rule
file adds coverage for nobody and removes it from nobody — purely additive bytes.**

---

## 6. The standing rules this leaves

1. **`seneschal/scripts/` stays flat.** The module docstrings are each script's authority; setup guides
   (`*_SETUP.md`, `INTEGRATIONS.md`) carry the long-form evidence.
2. **Do not propose a rule file or subdirectory to split scripts grounding.** The net is `floor +
   pointer` at any size (§5.2). If a win is wanted on over-long grounding, it is a **plain trim** of
   evidence whose fuller destination is verified — no new artifact.
3. **When checking whether evidence has a fuller home, look in three places**, not two: the module
   docstring, the spec named beside the entry, **and inline block comments at the code the evidence is
   about.** A trim that only compares docstring length will leave movable evidence behind.
4. **A new instruction file is created and budgeted in the same PR, or not created.** A sub-router or
   rule file outside the budget allowlist is not a smaller router, it is an unmeasured one; its budget
   is seeded at its **measured** size and the parent's budget drops by what actually left. The budget
   and pointer checks must scan `.claude/rules/**/*.md` before the first rule file lands, so that file
   — whoever writes it — is visible to both.
5. **Any future subdirectory needs an `__init__.py`**, a `<subject>_common.py`-style name, and a stated
   `±0` test delta (§3.2).
6. **Spell directory pointers with their full path** (`cockpit/breakglass/`, never the bare leaf), and
   never write a *forbidden* example as a backticked path — a pointer checker cannot tell a path being
   forbidden from a path being cited.

**What remains open, and is not settled by any of the above:** *navigation.* A flat directory of a
hundred-plus files is a human-browsing problem unrelated to when bytes load, and a rule file does
nothing for it. It is unmeasured; the instrument is the owner reporting that finding "which script does
X" got harder. A rule file may still be right for grounding that exists nowhere yet and is genuinely
path-specific — that is a different question, and this spec does not answer it.
