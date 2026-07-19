# Proteus — `state/`

**Runtime churn.** Everything here is rewritten every cycle, regenerable, and **gitignored**
(only this README and any `*.example.*` seeds are tracked). Nothing here is a deliverable.

This is the archon-wide convention — see [`seneschal/references/archons.md`](../../../seneschal/references/archons.md):

| dir | holds | in git? |
| --- | --- | --- |
| `state/` | runtime churn — ledgers, "latest" caches, counters, queues, sentinels, logs | no |
| `out/` | **deliverables** — drafted resumes/covers/research, digests, generated views | no (they embed the owner's contact details) |
| `<archon>/` | curated inputs + code — `profile.json`, `watchlist.json`, `voice-profile.md`, `tools/`, `.gitignore` | **yes** (private inputs as `*.example.*` seeds) |

Paths are defined once in [`tools/proteus_paths.py`](../tools/proteus_paths.py) — import that
rather than rebuilding them, so the tools can't drift apart.

## What lives here

| file | written by | what it is |
| --- | --- | --- |
| `seen.json` | `hunt_cycle.py` | **The dedup ledger** — `url → {first_seen, last_seen, best_score, notified, …}`. Decides what counts as a *new* match. **Losing this re-alerts every posting**, so the migration moves it rather than regenerating it. |
| `cycles.jsonl` | `hunt_cycle.py` | One append-only line per hourly cycle: `{at, fetched, kept, new_hot, notified, deferred, quiet}`. The daily digest rolls these up. |
| `jobs-latest.json` | `fetch_jobs.py` | Most recent raw fetch across the watchlist. Tens of MB, overwritten hourly. |
| `scored-latest.json` | `score_jobs.py` | Most recent scored pool — the live board. Tens of MB, overwritten hourly. |
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
