package com.kumouri.seneschal

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * POSTs buffered presence events (geofence enter/exit; later activity + sleep) as NDJSON to the desktop
 * listener's `/presence-ingest` over the tailnet. Fires on each geofence event, plus a ~15-minute periodic
 * flush in case a POST failed. Endpoint + optional bearer token come from BuildConfig (local.properties);
 * a blank endpoint fails fast instead of retrying forever (same contract as the other workers).
 */
class PresenceSyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        if (BuildConfig.PRESENCE_INGEST_URL.isBlank()) return Result.failure()
        val store = PresenceStore(applicationContext)
        val pending = store.drain()
        if (pending.isEmpty()) return Result.success()
        return try {
            if (post(pending)) {
                store.clearFirst(pending.size)
                Result.success()
            } else {
                Result.retry()
            }
        } catch (_: Exception) {
            Result.retry()
        }
    }

    private fun post(lines: List<String>): Boolean {
        val body = lines.joinToString("\n").toByteArray(Charsets.UTF_8)
        val conn = (URL(BuildConfig.PRESENCE_INGEST_URL).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            setRequestProperty("Content-Type", "application/x-ndjson")
            if (BuildConfig.PRESENCE_INGEST_TOKEN.isNotEmpty()) {
                setRequestProperty("Authorization", "Bearer ${BuildConfig.PRESENCE_INGEST_TOKEN}")
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

    companion object {
        private const val UNIQUE = "presence-sync"

        private fun connectedConstraints() =
            Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun enqueuePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<PresenceSyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(UNIQUE, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun syncNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<PresenceSyncWorker>()
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
    }
}
