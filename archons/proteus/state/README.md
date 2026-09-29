# Proteus — `state/`

**Runtime churn.** Everything here is rewritten every cycle, regenerable, and **gitignored**
(only this README and any `*.example.*` seeds are tracked). Nothing here is a deliverable.

This is the archon-wide convention — see [`seneschal/references/archons.md`](../../../seneschal/references/archons.md):

| dir | holds | in git? |
| --- | --- | --- |
| `state/` | runtime churn — ledgers, "latest" caches, counters, queues, sentinels, logs | no |
| `out/` | **deliverables** — drafted resumes/covers/research, digests, generated views | no (they embed the owner's contact details) |
| `<archon>/` | curated inputs + code — `profile.json`, `watchlist.json`, `voice-profile.md`, `company-intel.json`, `tools/`, `.gitignore` | **yes** (private inputs — `profile.json`, `watchlist.json`, `voice-profile.md`, `company-intel.json` — are gitignored, tracked only as `*.example.*` seeds; `company-intel.json` is written only by `promote_intel.py`) |

Paths are defined once in [`tools/proteus_paths.py`](../tools/proteus_paths.py) — import that
rather than rebuilding them, so the tools can't drift apart.

## What lives here

| file | written by | what it is |
| --- | --- | --- |
| `seen.json` | `hunt_cycle.py` (the only writer) | **The dedup ledger** — `url → {first_seen, last_seen, best_score, notified, apply_url, …}`. Decides what counts as a *new* match. **Losing this re-alerts every posting**, so the migration moves it rather than regenerating it. Append-only in practice, so it only grows. `apply_url` is carried so the daily digest can offer the same "apply here, not there" line the hourly alert does; it is **additive and sticky** — an older row simply has no key, and a later fetch that omits it doesn't erase a URL already learned. Read by `daily_digest.py`. |
| `cycles.jsonl` | `hunt_cycle.py` | One append-only line per hourly cycle: `{at, fetched, kept, new_hot, notified, deferred, quiet, hydration}`. `hydration` is the lazy-description step's summary (`{shortlisted, hydrated, rescored, warnings}`, or `{error}`). The daily digest rolls these up. |
| `jobs-latest.json` | `fetch_jobs.py` | Most recent raw fetch across the watchlist. Tens of MB, overwritten hourly. |
| `scored-latest.json` | `score_jobs.py`, then `hunt_cycle.py` | Most recent scored pool — the live board. Tens of MB, overwritten hourly. `hunt_cycle` rewrites it (atomically) only when the hydrate-and-rescore step changed a row. |
| `company-intel-pending.jsonl` | `record_intel.py` (append-only); pruned by `promote_intel.py` | **Proposed company intel awaiting promotion** — one JSON line per finding a work-up recorded (`{company, adjust, tags, note, source, updated, recorded_at}`). The archon appends here INSTEAD of writing the curated `company-intel.json`. `score_jobs.py` overlays it on the ledger so a finding tilts scoring the same cycle, marked `[pending review]`; `promote_intel.py --apply` (Dream step 2e) merges it into the local, gitignored `company-intel.json` and prunes a line only once its content has **landed** (refused, owner-weakening proposals stay queued). The opt-in `--via-pr` path (private forks only) lands via a PR instead, so a held or red PR never loses an entry. Deleting it discards every unpromoted finding. |
| `wttj-discovery.json` | `wttj_discover.py` | The last Welcome-to-the-Jungle discovery report — which companies post into US metros and how often, from the published sitemaps. An occasional curation aid, not part of the hunt; safe to delete. |
| `paused` | you (`touch`) | Sentinel. While it exists the hourly hunt exits immediately — the pause switch. |
| `logs/` | deploy/delegate commands | Archon deploy + A2A delegation logs and pid files. |
| `ledger.jsonl` | demiurge (`--ledger-dir`) | The delegation ledger — every delegated task's request/response. Runtime churn, deliberately **not** in the tracked stable (see `archons.md`). |
| `runs/<run-date>/` | `fetch_jobs.py` / `score_jobs.py` (`--out`) | **Per-run raw boards** — `jobs.json` (the fetch) + `scored.json` (the score) for one delegated run. Machinery, not deliverables: multi-MB apiece and regenerable from a re-fetch. The charter's Phase 1 hardcodes `--out .../out/<run-date>/jobs.json`, so these used to pile up in `out/` next to the resumes. `proteus_paths.resolve_raw_artifact()` now redirects them here — applied by **both** the writer (`--out`) and the reader (`--jobs`), so the charter's literal commands still chain. Safe to delete any time. **Retention: 7 days** (`hunt_cycle.py --prune-runs-days N`, 0 = keep forever) — they're forensic only ("why did you score this 84%?"), and nothing reads them. Not 24 h: the owner doesn't necessarily read a digest the day it's drafted, and a Tuesday work-up reviewed on Saturday would lose the board that produced it. Ages on the newest file in the dir, so a live run can't be pruned out from under itself. |

## Migration note

Runtime files used to sit in `out/hourly/` and loose in `out/`, mixed in with the deliverables.
`proteus_paths.migrate_legacy_state()` moves them here on the next run of any tool — idempotent,
self-healing, and deliberately in code rather than by hand because the hourly scheduled task races
any manual move.

**Second pass — the per-run boards.** The first migration caught the hourly caches but not
`out/<run-date>/{jobs,scored}.json`, because those come from the *charter's* Phase 1 rather than from
`hunt_cycle`. They kept accumulating: tens of MB across run dirs, one of them holding a double-digit-MB
raw board and a single 4 KB markdown file that was the actual deliverable. The sweep now moves those
into `runs/<run-date>/` too, leaves every other file untouched, refuses to clobber a newer `state/`
copy, and removes a run dir only once it holds nothing but churn.
