# Coverage manifests — a digest/doc names what it covers as file + line range + content hash, and goes stale by diff, not by discipline

**Status:** `SPEC-ONLY` — nothing here is built. Every phase below is unbuilt design, and the phases
table says so plainly rather than leaving a reader to find out. · **Owner:** the assistant.

**The ask:** spec a *"digest file + line range + hash"* mechanism for the repo's own digests and
documentation — so a summary that cites a place in a file can tell when that place has moved.

**Shapes copied deliberately:** measure before proposing; an early phase changes no behaviour; name
what it deliberately does not do; ship a measurement phase whole rather than a field at a time
(`CLAUDE.md` beside this file says so). `read-first-retirement-spec.md` — the classification shape,
and the same instruction this spec carries: design and record it, build nothing. The context checks'
report-only-then-`--enforce` ladder, and `check_context_pointers.py`'s rung-based path resolution,
reused directly below rather than re-invented.

**What this document does NOT do, and why that matters here specifically:** it proposes a syntactic
freshness primitive — does the cited text still exist, unchanged, at the cited place — never a
semantic one. Whether the *claim* built on that text is still *true* is a judgment-shaped problem;
mechanical checks aimed at it (tracing every figure in prose back to a source, say) drown in false
positives while catching the least consequential cases. Coverage manifests answer a narrower,
decidable question: a **syntactic property, decidable before anything is read for meaning.**

---

## 1. The problem

Grounding gets redone per agent, and a doc written "as we go" still drifts, because a summary carries
no signal of whether the thing it summarises has moved. Three shapes this repo already exhibits:

- **A written instruction with nothing checking it ran.** A generated-region mechanism is only as good
  as the code that re-renders it; the carry-over file's bounded regions (`carry-over-region-spec.md`)
  exist precisely because an earlier "rebuild from current state" instruction read correctly and was
  never actually wired. A correct, written-down instruction with no code observing whether it fired is
  a prose rule that does not bind.
- **A dated snapshot that is unverifiable as read.** An inventory whose rows each carry a `status: open`
  cell reads the same whether the fix for a row merged an hour later or never shipped at all. The
  document is correct as written and stale within hours, by construction.
- **Hand-refreshed grounding.** Every archon's context file is hand-maintained prose citing the live
  record (`archons/CLAUDE.md`); a refresh happens when someone remembers to audit it, and nothing tells
  a session that one of those files has drifted since its last audit.

The common shape: **a document makes a claim that used to be true of a specific place in a specific
file, and nothing downstream of the write knows when that stops being the case.** The sibling failure —
a correction stored where the failing turn doesn't look — is fixed by discovery. This is the other half:
a claim stored where **the thing it describes** doesn't look back at it.

---

## 2. The manifest — what it is, and the two open shapes it resolves

**Format: an HTML comment, not a fenced block.** A fenced code block renders visibly; an HTML comment
does not. This extends a convention the repo already runs: `check_rulings.py`'s own suppression
comment is exactly this shape — machine-readable, invisible to a human reader, parsed by a dedicated
regex. A coverage block is the same idea carrying structured data instead of one sentence:

```
<!-- coverage:
{"entries": [
  {"path": "seneschal/scripts/standing_safety.py", "lines": [11, 24],
   "sha256": "3f9a2b...", "written": "2026-01-01",
   "claim": "one gitignored JSON store, one owning script"}
]}
-->
```

**JSON inside the comment, not YAML.** The repo is stdlib-first (root `CLAUDE.md` → Conventions):
`json.loads` is stdlib; a YAML parser would be a new dependency for a format the repo has never needed
— every other tracked machine-data file (`reminder-aliases.example.json`,
`watch-suppressions.example.json`, `autonomy-config.json`) is plain JSON for the same reason.
`entries` is a list so one block can carry several claims about the same document (§2.1).

**Fields, matching `standing_safety.py`'s own field shape (`source`/`added`/`retire_when` → here
`path`/`lines`/`written`):**

| Field | Meaning |
|---|---|
| `path` | Repo-root-relative, exactly the convention `check_context_pointers.py`'s rung 0 resolves against first |
| `lines` | `[a, b]`, 1-indexed inclusive, or omitted entirely for a whole-file claim (§2.2) |
| `sha256` | Of the cited range's content at write time — computation defined in §3.1, because getting this wrong is a known landmine (below) |
| `written` | `YYYY-MM-DD`, so a reader (or a checker's report) can see how old a claim is without git blame |
| `claim` | Optional, short, the sentence this range is cited to support — present wherever the citing prose doesn't already make it obvious |

**The CRLF landmine this format must not repeat.** On a Windows checkout with `core.autocrlf=true` and
no `.gitattributes`, a file's worktree byte count differs from its git object by exactly its line
count, so a byte gate that doesn't pin which it counts is non-deterministic across platforms by ~1%.
§3.1 pins the hash the same way the byte-budget check pins its count — normalized, not worktree-raw.

### 2.1 Per-section vs per-document — resolved as neither pole

The question arrives as binary. The reading here is a third shape: **one manifest block per document
(or per named grounding section inside a large one, e.g. a sub-heading of `seneschal/SKILL.md`),
holding a list of independently addressed entries.** Granularity lives in the *entries*, not in the
*number of blocks* — a document with one claim gets one entry in one block; a router like `CLAUDE.md`
beside this file, whose lines each cite a different spec, gets one block with dozens of entries, each
pointing at its own `path`/`lines`. This avoids the block-sprawl a strict per-claim-per-block rule
would produce while still letting the checker (§3) report staleness at the claim's granularity rather
than "something in this whole 2,000-line file changed."

### 2.2 Hash granularity — range by default, whole-file for an entirety claim

**Range-level, hashing exactly the cited lines**, is the default: a claim should name precisely what
backs it, not a superset that happens to still be true. A range hash also keeps false positives low by
construction — an edit fifty lines away never flags it, where an unbounded scope is exactly what makes
figure-tracing checks noisy.

**Whole-file is the entry for a claim that is genuinely about the file's entirety** — e.g.
`archons/CLAUDE.md`'s "every archon has the same shape", citing an archon's whole context file rather
than one paragraph of it. `lines` omitted signals this; the hash is computed over every line, same
normalization as §3.1.

---

## 3. The checker — `check_coverage.py` (unbuilt)

For every manifest entry found in the tree, four possible verdicts, computed mechanically and never
requiring the checker to read for meaning:

- **FRESH** — the cited range's current content hashes to the stamped value.
- **MOVED** — the stamped hash does not match at the stamped range, but the *exact same content* (a
  byte-identical match after the §3.1 normalization) is found elsewhere in the same file. This is a
  "fuzzy re-anchor" that is deliberately not fuzzy in the sense of a diff or a similarity score — it is
  an exact-match search at a different offset, because anything looser reintroduces the false-positive
  failure mode. A bounded window first (the stamped range ± a fixed span), then a whole-file scan if
  the window misses, because most drift is a few lines inserted or deleted nearby.
- **STALE** — the path resolves (§3.2) but no exact match for the stamped text exists anywhere in the
  current file.
- **GONE** — the path itself does not resolve.

**MOVED is its own bucket, never folded into FRESH.** A citation whose line numbers are wrong is a real
defect even when the content it names is intact — the next reader following the pointer lands on the
wrong lines, which is exactly the "pointer into nothing, or into the wrong thing" failure
`check_context_pointers.py`'s own docstring names as the router system's real risk. A report that
counts MOVED as clean launders a citation error into a clean bill.

### 3.1 The hash computation — CRLF-normalized

`content.splitlines()` (universal-newline-aware, strips line endings regardless of CRLF/LF) → join the
cited range with a single `"\n"` → UTF-8 encode → `sha256`. The stamped hash is then identical whether
it was written on a CRLF checkout or read back on CI's LF one.

### 3.2 Path resolution — reused, not reinvented

`check_coverage.py` resolves a manifest entry's `path` through **the same rung ladder**
`check_context_pointers.py` already built and tuned: repo-root, then the containing document's
directory, then a declared base; tracked-or-about-to-be-tracked via `gate_git.working_tree_paths`; a
`.gitignore`-declared runtime path resolves to "exists on some host, not provably in this checkout"
rather than GONE. Re-deriving this ladder here would repeat the mistake it was built to fix — a naive
single-base recogniser reports hundreds of false findings on a clean tree.

### 3.3 What it must never do

**Never rewrite prose, never touch the `sha256`/`lines` fields itself.** A stale entry is a finding in
a report, exactly the posture `check_state_writes.py` and `check_context_pointers.py` both hold — each
finds a defect and stops there. A checker that "fixes" a stale manifest on its own is a new class of
risk: a wrong auto-correction is worse than a clearly-flagged stale claim, because the auto-correction
looks like diligence and isn't.

---

## 4. Who writes manifests, and what never gets one

**The writer of the artifact, at write time**, via a helper — `coverage_stamp.py` (unbuilt): given a
path and a line range (or none, for whole-file), it reads the current content, computes the §3.1 hash,
and emits the HTML-comment block ready to paste. It is a stamping tool, not a generator of claims — it
never invents the `claim` text, matching `standing_safety.py`'s posture that classification is a
decision made once at write time, never re-derived.

**Dream's nightly refresh re-stamps whatever it re-reads.** Dream already owns this shape of work for
`state/standing-safety.json` (`../modes/dream.md`, via the CLI, never by hand-editing) and for the
open-work register (`loops.py`) — a coverage re-stamp on the artifacts Dream regenerates nightly is the
same "the script that did the work writes the record" discipline `dream_steps.py`'s own docstring
states.

**A job's grounding step stamps its own notes** — the archon context-file refresh, made mechanical and
repeatable rather than something a session has to remember to do.

**First candidates, in the order they get manifests:**

1. The root and per-directory `CLAUDE.md` "what exists" claims — the router lines in `CLAUDE.md`
   beside this file, `seneschal/references/CLAUDE.md`, `archons/CLAUDE.md`, `cockpit/CLAUDE.md` — each
   already names a specific file and a specific behaviour; this is the population
   `check_context_pointers.py`'s `SCAN_GLOBS` already walks, so phase 0 (§6) measures against a corpus
   the repo already has tooling to enumerate.
2. The one-line status entries in the docs router (`BUILT`/`PARTIAL(...)`) — a manifest entry pointing
   at the code a behaviour lives in turns "PARTIAL(phase 0 BUILT)" from a claim resting on the reader's
   trust into one a checker can re-verify.
3. The archon grounding docs — a manifest here is the mechanical version of the hand audit.
4. Any dated snapshot doc whose rows name a file — a manifest entry per row turns "status: open" into
   something `check_coverage.py` can flag GONE/MOVED once the fix lands.
5. `state/standing-safety.json` items that cite a file by name — a manifest here catches a
   resolved-but-must-not-be-raised entry silently outliving the artifact that explains why
   (`read-first-retirement-spec.md`'s class B worry).

**Never get one, by design:**

- **Persona prose** (`persona/`) — the *wording* of persona and standing-safety lines is load-bearing
  voice (`grounding-restructure-spec.md`), not a claim about a place in code. Hashing it would flag
  every intentional rewrite as STALE with no signal, which is noise by construction.
- **Journal entries** — private narrative, not a claim about code or a file.
- **A `rulings.md` row, verbatim** — the row *is* the source of truth for that decision, not a pointer
  to one; there is nothing for it to drift from.

---

## 5. Corpus reconciliation — scoped later, named as what it is

Several agents each holding their own grounding corpus will eventually want something that merges
them — union of coverage, newest hash wins on a conflicting entry, a genuine disagreement surfaced
rather than silently resolved either direction. **This document only builds the primitive that would
need to exist first** — a manifest is a precondition for reconciliation, not a piece of it. Phase 3
names it and stops there.

---

## 6. Phases

Phase 0 ships as **one whole measurement** — the full FRESH/MOVED/STALE/GONE logic, not a subset: a
measurement phase that ships a field at a time buys a week and then discovers the rows can't answer
the question that made it worth building.

| Phase | What | Gated on | Tests it needs |
|---|---|---|---|
| **0** | `check_coverage.py`, report-only, full FRESH/MOVED/STALE/GONE logic (§3), run over the population `check_context_pointers.py`'s `SCAN_GLOBS` enumerates — **measures the standing staleness rate of file+line claims already present in prose today**, before a single manifest exists | nothing | a fixture tree with a known FRESH/MOVED/STALE/GONE entry each, plus a CRLF-vs-LF pair proving §3.1's normalization holds |
| **1** | `coverage_stamp.py` (§4) + manifests on the first artifact set (§4's five, in order) | phase 0's baseline exists | round-trip: stamp an entry, mutate the file, confirm the checker's verdict matches the mutation kind |
| **2** | `--enforce`, with an allowlist + required `reason`, the shape `check_state_writes.py` already runs (`seneschal/state-write-allowlist.json`) | phase 1's fire log shows the real noise floor on the tagged corpus — the same gate `check_context_pointers.py` held itself to before `--enforce` became CI's default | an allowlisted STALE entry never fails the build; a removed allowlist row re-fails it |
| **3** | Corpus reconciliation (§5) over several agents' manifests | phase 2 holding, and the owner's own scoping beyond what this document names | not designed here |

---

## 7. Open decisions — recommendation first

1. **Manifest placement.** §2.1 reads the binary framing as a third shape — one block per
   document/section, holding independently-addressed entries. **Recommendation: accept the third
   shape** — it avoids both block-sprawl and the loss of per-claim granularity, and costs nothing extra
   to build since the entries list already carries per-claim addressing.
2. **Hash granularity.** §2.2. **Recommendation: range by default, whole-file only for an entirety
   claim.**
3. **Does MOVED count as fresh?** §3. **Recommendation: no — report MOVED as its own bucket**; folding
   it into FRESH would let a citation drift for years while a report says everything is fine.
4. **Enforcement timing.** §6. **Recommendation: report-only through phase 1**, `--enforce` only once
   phase 1's fire log has run long enough to build a real allowlist — the same ladder
   `check_context_pointers.py` and `check_state_writes.py` already ran, both of which needed the wait to
   get their false-positive rate down before a red gate would have been disabled rather than fixed.

---

## 8. Deliberately not built

- **Auto-rewriting a stale manifest or the prose beside it.** §3.3.
- **A confidence score or staleness percentage on any entry.** A specific figure generated without
  measurement is the defect wearing a badge. FRESH/MOVED/STALE/GONE is a four-way enumeration, not a
  score.
- **Semantic staleness — "is the claim still true," not "does the cited text still match."** A much
  larger, judgment-shaped problem this document is not attempting.
- **Manifests on persona prose, journal entries, or a `rulings.md` row verbatim.** §4's "never" list.
- **Corpus reconciliation logic.** §5, explicitly phase 3 and not designed here.
- **Blocking anything on a stale manifest before phase 2.** Report-only through phase 1, by the same
  ladder every sibling check in this tree already ran.

---

## Router entry

**What it decides:** a digest/doc names what it covers as `path` + `lines` + a CRLF-normalized
`sha256`, in an HTML-comment JSON block (the `check_rulings.py` marker's shape, extended to carry
structured data); `check_coverage.py` (unbuilt) reports FRESH/MOVED/STALE/GONE per entry, reusing
`check_context_pointers.py`'s rung-based path resolution rather than re-deriving it, report-only until
an allowlist+reason mechanism exists. Never rewrites prose, never scores a claim's truth — only whether
the cited text still exists, unchanged, where it was cited.
