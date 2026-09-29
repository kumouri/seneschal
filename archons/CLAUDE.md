# archons/ — the assistant's Archon staff

**The one rule that exists nowhere else in this repo: an archon never writes a tracked file
directly.** It appends to a gitignored pending queue under its own `state/` and Dream promotes it
by PR (the shipped example: `archons/proteus/tools/record_intel.py` → `promote_intel.py`), so the
live checkout never goes permanently dirty — a dirty checkout blocks the daemon's ff-pull and arms
the pull-failed reload bug.

**The gitignore trap:** an archon that tracks an `out/README.md` must spell `out/*` plus
`!out/README.md` in BOTH gitignores — `archons/<id>/`'s own, and the repo root's net; git never
descends into an excluded directory, so `out/` with a re-include silently tracks nothing.

A zero exit from `demiurge delegate` is **not** success — an archon can return prose and stage
nothing. Judge the artifact.

This file loads when a turn touches this directory, instead of in every session unasked. The
lifecycle, the demiurge commands and the full layout rules: `seneschal/references/archons.md`.

---

archons/           the Archon staff data (Forge mode). Per install: `need/` (need statements) +
`stable/` (spec/charter/evals/record per archon) are the owner's own and gitignored in this public
framework; `scaffolds/` (generated) is always gitignored. The delegation **ledger** is NOT in the
stable: it's the one part that churns (demiurge appends per delegation, storing full
request/response), so it lives gitignored at `archons/<id>/state/ledger.jsonl` via `demiurge
--ledger-dir` — tracking it would leave the live checkout permanently dirty (same bug) and write the
owner's operational context into git history.

Every archon has the same shape (`seneschal/references/archons.md` → "Archon internal layout"):
tracked inputs + `tools/` (only if it HAS tools — an empty dir is scaffolding for its own sake) +
its OWN `.gitignore`; **`state/`** = runtime churn (ledgers, latest-caches, queues, sentinels, logs —
gitignored except `README.md` and `*.example.*`) and **`out/`** = deliverables (drafts, digests,
generated views — gitignored; they carry the owner's personal data), except a README where one
exists (mind the trap above). Private inputs (`profile.json`, `watchlist.json`, `voice-profile.md`)
are gitignored; only their `*.example.*` twins are tracked.

The shipped example is **`proteus/`** (a job-application archon): `profile.example.json`, `tools/`
(stdlib tools — `tools/proteus_paths.py` is the single source of truth for its paths), and a
`state/README.md`. A UI-having archon ALSO ships a tracked `site.json` at its root — the presence
daemon's `archon_sites.py` self-discovers it and keeps the site up.
