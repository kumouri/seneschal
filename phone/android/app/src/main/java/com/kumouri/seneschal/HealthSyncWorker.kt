package com.kumouri.seneschal

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.MealType
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit
import java.util.concurrent.TimeUnit

/**
 * Reads Samsung Health data out of Health Connect and POSTs it as NDJSON to the desktop listener
 * (health_listener.py) over the tailnet. This is the "genuinely automatic" feed — no manual export tap.
 *
 * Runs every ~3 hours (and on demand). Endpoint + optional bearer token come from BuildConfig, which
 * reads them from local.properties at build time (see the android README). Idempotent: every record
 * carries Health Connect's stable id, and the desktop upserts on it, so overlapping re-reads never
 * duplicate. We deliberately re-read the last GRACE_DAYS on each run so same-day step totals stay correct.
 *
 * Also reads ExerciseSessionRecord (workouts) and NutritionRecord (meals). A workout's calories/distance
 * aren't fields on the session record itself -- Health Connect stores them as separate
 * TotalCaloriesBurnedRecord / DistanceRecord entries covering the same interval -- so we read those too
 * and fold in whatever overlaps a given session's [start, end).
 *
 * The wire format is documented in seneschal/scripts/health_import.py (import_ndjson). Nothing here reads
 * or sends anything but the owner's own health data to their own desktop.
 */
class HealthSyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        // Not configured yet (no tailnet endpoint baked in) — fail fast instead of retrying forever.
        if (BuildConfig.HEALTH_INGEST_URL.isBlank()) return Result.failure()
        if (HealthConnectClient.getSdkStatus(applicationContext) != HealthConnectClient.SDK_AVAILABLE) {
            return Result.failure()  // Health Connect isn't installed/available; nothing we can do.
        }
        val client = HealthConnectClient.getOrCreate(applicationContext)

        val granted = client.permissionController.getGrantedPermissions()
        if (!granted.containsAll(READ_PERMISSIONS)) {
            return Result.success()  // User hasn't granted reads yet; MainActivity asks. Don't retry-spam.
        }

        return try {
            val prefs = applicationContext.getSharedPreferences("health_sync", Context.MODE_PRIVATE)
            val now = Instant.now()
            val watermark = Instant.ofEpochMilli(
                prefs.getLong("watermark_ms", now.minus(30, ChronoUnit.DAYS).toEpochMilli())
            )
            // Re-read a couple days of overlap so same-day step totals and late sleep edits stay correct.
            val since = watermark.minus(GRACE_DAYS, ChronoUnit.DAYS)
            val range = TimeRangeFilter.between(since, now)

            val lines = ArrayList<String>()
            readSleep(client, range, lines)
            readHeartRate(client, range, lines)
            readSpo2(client, range, lines)
            readSteps(client, range, lines)
            readWorkouts(client, range, lines)
            readNutrition(client, range, lines)
            if (lines.isEmpty()) return Result.success()

            if (!post(lines)) return Result.retry()
            prefs.edit().putLong("watermark_ms", now.toEpochMilli())
                .putLong("last_sync_ms", System.currentTimeMillis()).apply()
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    // --- readers: each appends NDJSON lines matching import_ndjson's record shapes ---

    private suspend fun readSleep(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val records = c.readRecords(ReadRecordsRequest(SleepSessionRecord::class, range)).records
        for (r in records) {
            val off = offsetMinutes(r.startZoneOffset, r.startTime)
            val stages = JSONArray()
            for (s in r.stages) {
                val name = STAGE_NAMES[s.stage] ?: continue
                stages.put(JSONArray().put(name).put(s.startTime.toEpochMilli()).put(s.endTime.toEpochMilli()))
            }
            out.add(
                JSONObject()
                    .put("t", "sleep_session").put("uuid", r.metadata.id)
                    .put("start_ms", r.startTime.toEpochMilli()).put("end_ms", r.endTime.toEpochMilli())
                    .put("offset_min", off).put("stages", stages).toString()
            )
        }
    }

    private suspend fun readHeartRate(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val records = c.readRecords(ReadRecordsRequest(HeartRateRecord::class, range)).records
        for (r in records) {
            val off = offsetMinutes(r.startZoneOffset, r.startTime)
            for (s in r.samples) {
                out.add(
                    JSONObject()
                        .put("t", "hr").put("uuid", "${r.metadata.id}:${s.time.toEpochMilli()}")
                        .put("start_ms", s.time.toEpochMilli()).put("offset_min", off)
                        .put("bpm", s.beatsPerMinute).toString()
                )
            }
        }
    }

    private suspend fun readSpo2(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val records = c.readRecords(ReadRecordsRequest(OxygenSaturationRecord::class, range)).records
        for (r in records) {
            out.add(
                JSONObject()
                    .put("t", "spo2").put("uuid", r.metadata.id)
                    .put("start_ms", r.time.toEpochMilli()).put("offset_min", offsetMinutes(r.zoneOffset, r.time))
                    .put("pct", r.percentage.value).toString()
            )
        }
    }

    private suspend fun readSteps(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val records = c.readRecords(ReadRecordsRequest(StepsRecord::class, range)).records
        // steps_daily is keyed by local date, so sum the interval records into per-date totals.
        val byDate = HashMap<String, Long>()
        for (r in records) {
            val zone = r.startZoneOffset ?: ZoneId.systemDefault().rules.getOffset(r.startTime)
            val date = r.startTime.atZone(zone).toLocalDate().toString()
            byDate[date] = (byDate[date] ?: 0L) + r.count
        }
        for ((date, count) in byDate) {
            out.add(JSONObject().put("t", "steps").put("date", date).put("count", count).toString())
        }
    }

    private suspend fun readWorkouts(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val sessions = c.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, range)).records
        if (sessions.isEmpty()) return
        // Calories/distance aren't fields on the session -- they're separate records over the same
        // interval -- so pull both once for the whole range and fold in whatever overlaps each session.
        val calories = c.readRecords(ReadRecordsRequest(TotalCaloriesBurnedRecord::class, range)).records
        val distances = c.readRecords(ReadRecordsRequest(DistanceRecord::class, range)).records
        for (r in sessions) {
            val off = offsetMinutes(r.startZoneOffset, r.startTime)
            val kcal = calories.filter { overlaps(it.startTime, it.endTime, r.startTime, r.endTime) }
                .sumOf { it.energy.inKilocalories }
            val meters = distances.filter { overlaps(it.startTime, it.endTime, r.startTime, r.endTime) }
                .sumOf { it.distance.inMeters }
            val obj = JSONObject()
                .put("t", "workout").put("uuid", r.metadata.id)
                .put("start_ms", r.startTime.toEpochMilli()).put("end_ms", r.endTime.toEpochMilli())
                .put("offset_min", off)
                .put("exercise_type", ExerciseSessionRecord.EXERCISE_TYPE_INT_TO_STRING_MAP[r.exerciseType] ?: "unknown")
            r.title?.let { obj.put("title", it) }
            r.notes?.let { obj.put("notes", it) }
            if (kcal > 0.0) obj.put("energy_kcal", kcal)
            if (meters > 0.0) obj.put("distance_m", meters)
            out.add(obj.toString())
        }
    }

    private suspend fun readNutrition(c: HealthConnectClient, range: TimeRangeFilter, out: MutableList<String>) {
        val records = c.readRecords(ReadRecordsRequest(NutritionRecord::class, range)).records
        for (r in records) {
            val off = offsetMinutes(r.startZoneOffset, r.startTime)
            val obj = JSONObject()
                .put("t", "nutrition").put("uuid", r.metadata.id)
                .put("start_ms", r.startTime.toEpochMilli()).put("end_ms", r.endTime.toEpochMilli())
                .put("offset_min", off)
                .put("meal_type", MealType.MEAL_TYPE_INT_TO_STRING_MAP[r.mealType] ?: "unknown")
            r.name?.let { obj.put("name", it) }
            r.energy?.let { obj.put("energy_kcal", it.inKilocalories) }
            r.protein?.let { obj.put("protein_g", it.inGrams) }
            r.totalCarbohydrate?.let { obj.put("carbs_g", it.inGrams) }
            r.totalFat?.let { obj.put("fat_g", it.inGrams) }
            out.add(obj.toString())
        }
    }

    /** Half-open interval overlap: does [aStart, aEnd) share any instant with [bStart, bEnd)? */
    private fun overlaps(aStart: Instant, aEnd: Instant, bStart: Instant, bEnd: Instant): Boolean =
        aStart.isBefore(bEnd) && bStart.isBefore(aEnd)

    private fun post(lines: List<String>): Boolean {
        val body = lines.joinToString("\n").toByteArray(Charsets.UTF_8)
        val conn = (URL(BuildConfig.HEALTH_INGEST_URL).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            setRequestProperty("Content-Type", "application/x-ndjson")
            if (BuildConfig.HEALTH_INGEST_TOKEN.isNotEmpty()) {
                setRequestProperty("Authorization", "Bearer ${BuildConfig.HEALTH_INGEST_TOKEN}")
            }
            connectTimeout = 15_000
            readTimeout = 30_000
        }
        return try {
            conn.outputStream.use { it.write(body) }
            conn.responseCode == 200
        } finally {
            conn.disconnect()
        }
    }

    private fun offsetMinutes(offset: ZoneOffset?, at: Instant): Int =
        (offset ?: ZoneId.systemDefault().rules.getOffset(at)).totalSeconds / 60

    companion object {
        private const val UNIQUE = "health-sync"
        private const val GRACE_DAYS = 2L

        // Samsung Health -> Health Connect stage codes we care about.
        private val STAGE_NAMES = mapOf(
            SleepSessionRecord.STAGE_TYPE_DEEP to "deep",
            SleepSessionRecord.STAGE_TYPE_LIGHT to "light",
            SleepSessionRecord.STAGE_TYPE_REM to "rem",
            SleepSessionRecord.STAGE_TYPE_SLEEPING to "light",
            SleepSessionRecord.STAGE_TYPE_AWAKE to "awake",
            SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED to "awake"
        )

        /** The read permissions this worker needs. MainActivity requests exactly this set. */
        val READ_PERMISSIONS: Set<String> = setOf(
            HealthPermission.getReadPermission(SleepSessionRecord::class),
            HealthPermission.getReadPermission(HeartRateRecord::class),
            HealthPermission.getReadPermission(OxygenSaturationRecord::class),
            HealthPermission.getReadPermission(StepsRecord::class),
            HealthPermission.getReadPermission(ExerciseSessionRecord::class),
            HealthPermission.getReadPermission(NutritionRecord::class),
            HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class),
            HealthPermission.getReadPermission(DistanceRecord::class)
        )

        private fun connectedConstraints() =
            Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun enqueuePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<HealthSyncWorker>(3, TimeUnit.HOURS)
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(UNIQUE, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun syncNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<HealthSyncWorker>()
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
    }
}
