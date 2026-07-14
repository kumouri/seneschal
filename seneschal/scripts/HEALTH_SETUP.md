# Samsung Health → the assistant: import, dashboard, and how far "automatic" actually goes

If the owner wears a Galaxy Watch, Samsung Health records their sleep well enough but **shows** it badly:
the app surfaces one "main sleep" block a night and buries everything else. For a sleeper whose nights are
heavily fragmented — two or more separate sessions — the app's summary is not a rounding error, it's a
different story than the data tells. This pipeline pulls the export into a local SQLite cache and renders
an **actigram**: one row per night, the full clock, every session where it actually happened.

Stdlib only, no packages to install:

| Script | Does |
|--------|------|
| `health_common.py` | CSV reading, the **timezone contract**, the night boundary, the schema |
| `health_import.py` | Samsung export (CSV zip + per-minute JSON zip) → `../state/health.db` (idempotent); also the live-feed NDJSON path |
| `health_dashboard.py` | `health.db` → `../state/health-dashboard.html` (interactive, self-contained, no CDN) |
| `health_listener.py` | tailnet HTTP receiver for a phone's Health Connect feed → import |
| `test_health.py` | `python -m unittest test_health` — guards the timezone contract, the live feed, the missed-sleep detector |

Both the cache and the rendered page live in gitignored `seneschal/state/`. Nothing here sends anything
anywhere: import reads files the owner already exported (or their own phone sends), the dashboard writes a
local HTML file, the listener only receives. **Act-low.**

## Quick start

```bash
cd seneschal/scripts
# the CSV zip carries the summaries; the JSON zip carries the per-minute movement + heart rate.
python health_import.py --zip ~/Downloads/samsunghealth_export_YYYYMMDDHHMMSS.zip --jsons ~/Downloads/jsons.zip
python health_dashboard.py            # -> seneschal/state/health-dashboard.html
```

Then open the HTML from disk. Pass `--jsons` to fold in the per-minute export (movement + heart rate);
without it you still get the full sleep/vitals dashboard, just no "missed sleep" layer. The dashboard is
**interactive** — the stat tiles and trend charts recompute for any span you pick (preset buttons or the
two date inputs), all client-side. `--days N` limits how many recent nights the page loads (default: all).
`python health_import.py --status` reports what the cache holds.

## The timezone contract — read this before touching the data

Samsung stores `start_time` / `end_time` / `create_time` / `update_time` as **UTC**, and puts the local
offset in a separate `time_offset` column (e.g. `UTC-0600` in winter, `UTC-0500` in summer for US
Central). The strings *look* like wall clock. They are not.

Read them as wall clock and every event lands hours late — precisely the size of the owner's UTC offset.
That is not a subtle error: it can move a 03:00 bedtime to 09:00 and turn an 07:50 wake-up into early
afternoon. **Any conclusion of the form "Samsung is detecting my sleep about five and a half hours late"
is what this bug looks like from the outside.**

The data settles it, without appealing to Samsung's docs. Hourly-binned `tracker.heart_rate` rows on the
US DST transitions (from the reference dataset this pipeline was built against):

| Date | If strings were **local** | If strings were **UTC** | What's actually there |
|------|---------------------------|--------------------------|----------------------|
| 2026-03-08 (spring forward) | hour `02` cannot exist | all 24 hours present | **24 bins, `02` present** |
| 2025-03-09 (spring forward) | hour `02` cannot exist | all hours present | **`02` present** |
| 2025-11-02 (fall back) | hour `01` appears twice | no duplicate | **no duplicate** |

So: UTC. `health_common.parse_offset` + `to_local` do the conversion, and `test_health.py` locks it down.

Two exceptions worth knowing:

* `day_time` on the daily-summary tables (`step_daily_trend`, `activity.day_summary`) is **already local
  midnight**, stored as an epoch-as-if-UTC. Read it as a plain date — `day_time_to_date` does.
* `medication.log.dosage_date` is the same shape: epoch-ms of local midnight.

## The night boundary

A "night" runs **19:00 → 19:00 local**, keyed by the date it opens (`sleep_day`, `CUT_HOUR`). The textbook
noon→noon actigraphy window is wrong for an owner who regularly sleeps across noon. Measured across the
reference dataset, 19:00 was the hour *least* likely to contain sleep, so it is the cut that severs the
fewest real sleeps. Spans crossing the cut are **split**, not clipped, so the actigram's painted area
equals the in-bed minutes.

Totals are the **union** of overlapping sessions, never their sum: on a handful of nights the watch and the
phone can both log the same sleep, and summing `duration_min` would invent hours that never happened.

## What the CSV export actually contains

~79 CSVs. Line 1 is a provenance comment; **the header is line 2**. The pipeline imports the nine tables
that matter (sleep sessions, sleep stages, heart rate, stress, SpO₂, skin temperature, daily steps,
medication log, weight) and ignores food, badges, social, and the rest.

What the **CSV** zip doesn't contain — but the **JSON** zip does:

* **The per-minute series lives in the JSON export.** In the CSV, `heart_rate` is only *hourly bins* and
  `movement`/`hrv` are bare pointers to `binning_data` files. Samsung's "Download personal data" actually
  offers those files as a **separate** zip (`jsons.zip` here): thousands of movement and heart-rate files,
  each a list of 60-second bins with absolute epoch-ms timestamps. `health_import.py --jsons` folds them
  in — hundreds of thousands of per-minute points — into the `movement` and `hr_minute` tables. This is
  what powers the dashboard's **green "missed sleep" layer**: a stretch where the owner was still
  (`activity_level` low) *and* at their sleeping heart rate (`hr` low) with **no Samsung session** is
  almost certainly sleep the app dropped. The heart-rate gate is load-bearing — it's what stops daytime
  stillness (sitting at the computer) from lighting up. On the reference dataset that surfaced dozens of
  hours of likely sleep Samsung logged as nothing.
* **The JSON has no offset column.** Its timestamps are absolute UTC epoch-ms, correct and unambiguous, but
  to place them on the local actigram the importer joins each binning file to its CSV parent's recorded
  `time_offset` (so travel days stay right), falling back to the owner's timezone at that instant
  (`tz_common.offset_minutes` — the configured `persona/identity.json` zone, else machine-local) when a
  file has no CSV parent.
* **Fine-grained pedometer data reaches back ~30 days.** Older days survive only as daily totals.
* Samsung's own `bedtime_detection_delay` / `wakeup_time_detection_delay` columns are imported — Samsung's
  own account of how late it notices sleep onset/wake (median in the reference data: 16 and 22 minutes),
  which is what disproved an initial "hours late" hypothesis.

Even with the per-minute data, HRV/respiratory/snoring series in the JSON export aren't imported yet —
they're there for the taking if a later question needs them.

## Automating the export

### What is not possible

**Samsung offers no API and no scheduled export.** The personal-data zip comes from a manual tap inside
the phone app (Settings → About/Personal data → Download personal data). There is no endpoint to cron, no
OAuth, no desktop hook. The Samsung Health Data SDK exists but is Android-only and gated behind partner
registration. `adb pull` doesn't help either: the Samsung Health database lives in `/data/data/…`, which is
unreadable without root.

Anything claiming to automate the *zip* is really automating something downstream of it.

### Tier 0 — automate everything after the tap (works today)

Point the importer at wherever the zip lands. It records each export's id and skips ones it has already
seen, so it is safe to run on a timer:

```bash
python health_import.py --watch-dir ~/Downloads     # imports anything new, else exits quietly
python health_dashboard.py
```

`run-health-refresh.cmd` wraps both. Register it once:

```powershell
schtasks /Create /TN "seneschal-health-refresh" /SC DAILY /ST 22:30 `
  /TR "%USERPROFILE%\workspace\repos\seneschal\seneschal\scripts\run-health-refresh.cmd"
```

The owner taps "Download personal data" when they feel like it (monthly is plenty); the cache and the
dashboard keep themselves current. This is the whole manual burden: **one tap, occasionally.**

> Alternatively, fold the same two commands into the nightly **Dream** run, which already does act-low
> local cache refreshes (`seneschal/SKILL.md` → Dream). No new scheduled task, one fewer moving part.

### Tier 1 — genuinely automatic, via Health Connect (transport = Tailscale/LAN)

Samsung Health syncs to **Health Connect** on the phone. An Android app holding Health Connect read
permissions can pull `SleepSessionRecord` (with stages), `HeartRateRecord` (**per-sample**),
`StepsRecord`, and `OxygenSaturationRecord` on a `WorkManager` schedule and ship them to the desktop. No
tap, and finer data than the zip. The chosen transport is **Tailscale/LAN** — no cloud; the phone POSTs
straight to a desktop listener over the tailnet.

The desktop end ships in this repo:

* **`health_listener.py`** — a stdlib HTTP receiver that **binds the tailnet interface only** (Tailscale's
  `100.64.0.0/10`; it refuses a public bind without a token). It archives each batch to
  `state/health-feed.ndjson` and imports it into `health.db`. `POST /health-ingest`, `GET /health`.
* **`import_ndjson`** in `health_import.py` lands the feed in the **same tables** the zip importer writes,
  so the dashboard is identical whichever way the data arrived.

The phone end is a companion Android app (**not included in this repo**): a `WorkManager` worker that,
every ~3h, reads Health Connect since a watermark (re-reading the last 2 days so same-day step totals stay
correct), emits NDJSON (the format `import_ndjson` expects), and POSTs it with an optional bearer token —
idempotent via Health Connect's stable record ids. Any client that can POST that NDJSON works. Approve the
Health Connect read permissions on the phone, then start the listener first:

```bash
python health_listener.py --token <pick-a-token>   # binds your Tailscale IP, port 8765
```

The two other transports considered, for the record:

| | Where data rests | New infra | Notes |
|-|------------------|-----------|-------|
| **A. Cloud relay (e.g. a Worker)** | object storage/DB, then pulled to `health.db` | `/health-ingest` + secret | Most robust, works off-LAN |
| **B. Tailscale / LAN ✅** | straight to the desktop | the listener above | **Chosen.** No cloud; phone must reach the desktop when the job fires |
| **C. Synced folder** | Syncthing/Drive folder → `--watch-dir` | none | Zero backend; leans on a third-party sync app |

The trade-off with B: the phone must reach the desktop when the sync fires, so a night the desktop is
unreachable waits for the next successful run (WorkManager retries; a worker can also run on-demand from
the app). A 2-day re-read window means nothing is lost, just delayed.

## Schema

`seneschal/state/health.db`, created on demand. Times are naive-UTC ISO strings plus `tz_offset_min`, so
any consumer can render local without guessing.

| Table | Grain | Notes |
|-------|-------|-------|
| `exports` | one per imported zip | dedupe key; re-importing is a no-op |
| `sleep_session` | one per Samsung session | `sleep_day` = the 19:00-anchored night; keeps Samsung's own detection-delay columns |
| `sleep_stage` | one per stage span | `awake` / `light` / `deep` / `rem`; `sleep_id` → `sleep_session.datauuid` |
| `heart_rate` | hourly bin **or** spot reading | `binned` distinguishes them |
| `stress`, `spo2`, `skin_temp` | one per measurement window | |
| `steps_daily` | one per local date | `source_type = -2` (all-sources rollup) preferred |
| `medication_log`, `weight` | one per entry | |
| `movement` | one per 60s bin (JSON export) | `activity_level`; the actigraphy signal |
| `hr_minute` | one per 60s bin / sample | per-minute heart rate (JSON export / live feed) |

Rebuild from scratch any time — it's a cache, and the export is cumulative:

```bash
rm seneschal/state/health.db && python health_import.py --watch-dir ~/Downloads && python health_dashboard.py
```
