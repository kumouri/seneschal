# Seneschal Call Shield (Android on-device blocker)

A tiny Kotlin app that **silences calls on the screener's blocklist before your phone rings.**
Everything else (contacts, unknowns) rings normally — so the cloud screener still handles the
unknowns you miss, and when the screener flags a new spammer it syncs here and that number never rings again.

```
incoming call → this app checks the synced blocklist
   ├─ on blocklist → silently rejected (no ring, no notification, still logged)
   └─ otherwise    → rings normally  → (unanswered) → forwards to the screener → it screens/learns → syncs back
```

The app pulls the blocklist hourly from `GET /blocklist` on your Worker. Block action is **silent reject**.

This folder is a **committed, buildable Gradle project** — no manual project setup. Open it in Android
Studio, or build from the command line with the Gradle wrapper.

## Prereqs
- **JDK 17** (the wrapper targets Gradle 8.14.x / AGP 8.11). Android Studio's bundled JBR works too.
- The **Android SDK** (Android Studio installs it). The build uses `compileSdk 36` / `build-tools 36.1.0`.
- Your `BLOCKLIST_SYNC_SECRET` — the value you set on the Worker with
  `wrangler secret put BLOCKLIST_SYNC_SECRET` (keep a local copy in `phone/.dev.vars`, gitignored).

## Build & install

**1. Create `phone/android/local.properties`** (gitignored — SDK path + endpoints/secrets):
```properties
sdk.dir=C:/Users/<you>/AppData/Local/Android/Sdk
BLOCKLIST_URL=https://seneschal-screener.YOUR-SUBDOMAIN.workers.dev/blocklist
BLOCKLIST_SECRET=<paste your BLOCKLIST_SYNC_SECRET>
```
Opening the project in Android Studio writes `sdk.dir` for you; you add the endpoints. The endpoints are
baked into `BuildConfig` at build time — **empty values still compile**, the app just won't sync until you
fill them. (Health-feed endpoints go here too — see below.)

**2. Build + install** on the phone (USB or wireless debugging on):
```bash
cd phone/android
./gradlew installDebug        # builds, signs with the debug keystore, installs over adb
# or: open the folder in Android Studio and hit Run ▶
```

**3. Grant the role.** Open the app → **"Make Seneschal the screening app"** → approve. (This takes over
the "Caller ID & spam app" role from your OEM's spam app, e.g. Samsung Smart Call — only one app can
hold it.)

**4. Keep it alive.** Settings → Apps → Seneschal Call Shield → Battery → **Unrestricted** (so the OS
doesn't pause the hourly sync).

## Test it
The blocklist starts empty, so seed one number you can call from:
```bash
wrangler d1 execute call_screener --remote --command \
  "INSERT INTO blocklist (number_e164, reason, confidence, first_seen, last_seen, hit_count)
   VALUES ('+1XXXXXXXXXX','manual test',1.0,datetime('now'),datetime('now'),1)"
```
Then in the app tap **Sync blocklist now** (count should show 1) → call your cell from that number →
it should be **silently rejected** (no ring), and appear in your call log as a blocked/declined call.

## Notes
- Only one app can hold the call-screening role — granting it to this app replaces your OEM spam app's role.
- `onScreenCall` runs for non-contact calls; contacts ring through regardless (which is what we want).
- The secret in `BuildConfig` is extractable from the APK — fine for a personal sideloaded app (it only
  grants read access to your blocklist). Don't publish the APK with it baked in.
- CI builds this app on every change under `phone/android/**` (`.github/workflows/android.yml`,
  `./gradlew :app:assembleDebug`) — with no secrets, since `BuildConfig` fields default to `""`.

---

## Health feed (Health Connect → desktop dashboard)

The same app also carries `HealthSyncWorker`, which reads health data out of **Health Connect** and
POSTs it as NDJSON to the desktop listener (`seneschal/scripts/health_listener.py`) over your **tailnet** —
the "genuinely automatic" sleep feed, no manual export tap. It reuses the existing background-sync shape
(`BlocklistSyncWorker`), runs every ~3 hours, and lands in the same `health.db` the dashboard reads. Full
picture: `seneschal/scripts/HEALTH_SETUP.md`.

**v4: workouts + nutrition.** The worker also reads `ExerciseSessionRecord` (workouts) and
`NutritionRecord` (meals) and ships them over the same NDJSON pipe as `t: "workout"` / `t: "nutrition"`
lines — see `import_ndjson`'s docstring in `health_import.py` for the exact wire shapes. A workout's
calories/distance aren't fields on the session itself in Health Connect — they're separate
`TotalCaloriesBurnedRecord` / `DistanceRecord` entries over the same interval — so the worker also reads
those and folds in whatever overlaps a given session's start/end before posting.

Everything the health feed needs is already in this committed project — the Health Connect dependency, the
`android.permission.health.READ_*` permissions (sleep, heart rate, oxygen saturation, steps, exercise,
total calories burned, distance, nutrition), the `<queries>` block, and the permissions-rationale
`activity-alias`. You only add the endpoint + token to `local.properties`.

**1. Desktop side first.** On the machine that holds `health.db`, run the listener (it binds your Tailscale
IP by default):
```bash
python seneschal/scripts/health_listener.py --token <pick-a-token>
# prints e.g.  health listener on http://100.x.y.z:8765  (tailnet, token required)
```
Note the `http://100.x.y.z:8765` address — that's your `HEALTH_INGEST_URL` below. (For a first smoke test
without Tailscale, `--host 127.0.0.1` and point the phone at the PC's LAN IP.)

**2. Add the endpoint + token to `local.properties`** (the same file as above, gitignored):
```properties
HEALTH_INGEST_URL=http://100.x.y.z:8765/health-ingest
HEALTH_INGEST_TOKEN=<the same token you passed to health_listener.py>
```

**3. Build + install** (`./gradlew installDebug`, or Run ▶). On launch the app requests Health Connect read
access through Health Connect's own system dialog — **approve** sleep, heart rate, blood oxygen, steps,
exercise, total calories burned, distance, nutrition, and (for hands-off background syncing)
allow-all-the-time. Then **Sync now** fires the first pull; after that it's automatic.

**4. Verify.** The listener prints a line per ingest (`[time] ingest NB -> {...}`). Then rebuild the
dashboard on the desktop: `python seneschal/scripts/health_dashboard.py`.

### Notes
- **Health Connect is the source.** Make sure your health app (e.g. Samsung Health) is syncing into
  Health Connect on the phone. Watch/phone data flows health app → Health Connect →
  this worker → your desktop.
- **Background reads** need the "all the time" grant *and* the app kept off battery-optimization (same
  Unrestricted setting as the blocklist sync). Without it, the sync still runs on app-open and while
  charging — just not on its own overnight.
- **Idempotent + self-healing.** Every record carries Health Connect's stable id; the desktop upserts on
  it, and the worker re-reads the last 2 days each run so same-day step totals and late sleep edits stay
  correct. Re-syncing never duplicates.
- The token in `BuildConfig` is extractable from the APK — fine for a personal sideloaded app on a private
  tailnet. The listener also refuses to bind a public interface without a token.

---

## Presence feed (Phase 1 — geofences)

The app also carries the **presence feed**: it registers on-device **geofences** for your named places
and POSTs enter/exit **events** to the desktop listener's `/presence-ingest` over the tailnet, so the
assistant can time things off "arrived home" / "left." **Coordinates never leave the phone** — the wire carries only the
place name + `enter`/`exit`.

**1. Endpoint in `local.properties`** (same listener as the health feed, different path + the same token):
```properties
PRESENCE_INGEST_URL=http://100.x.y.z:8765/presence-ingest
PRESENCE_INGEST_TOKEN=<the same token you passed to health_listener.py>
```

**2. On device** (`./gradlew installDebug`, then in the app):
- Tap **"Save this spot as a place"** → grant **location** when asked, then name it (default `home`).
- Android then sends you to Settings to choose **"Allow all the time"** for background location — do it, or
  geofences only fire while the app is open.
- Battery → **Unrestricted** (same as the other feeds).

Verify on the desktop: the listener prints `… /presence-ingest …` on each enter/exit;
`python presence_import.py --status` / `--context` show the event store + the current `{at_place, …}`.

---

## Project layout

```
phone/android/
  settings.gradle.kts            module list (:app)
  build.gradle.kts               plugin versions (AGP 8.11, Kotlin 2.1)
  gradle.properties              AndroidX + JVM args
  gradle/wrapper/                the Gradle wrapper (8.14.x) — committed
  gradlew, gradlew.bat           wrapper launchers
  local.properties              (gitignored) SDK path + BuildConfig endpoints/secrets
  app/
    build.gradle.kts             module config, deps, BuildConfig wiring
    proguard-rules.pro
    src/main/
      AndroidManifest.xml
      java/com/kumouri/seneschal/    CallScreeningServiceImpl, BlocklistStore, BlocklistSyncWorker,
                                 HealthSyncWorker, MainActivity
      res/                       layout, strings, launcher icons
```

> Historically this folder shipped **source-only** — you generated an Android Studio project and pasted the
> files in. That step is gone: the Gradle project (wrapper included) is committed, so building is just
> `local.properties` + `./gradlew installDebug`.
