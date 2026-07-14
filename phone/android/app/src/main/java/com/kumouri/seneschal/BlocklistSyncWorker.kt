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
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * Pulls the screener's blocklist from the Worker's GET /blocklist and caches it locally.
 * Runs hourly (and on demand). URL + bearer secret come from BuildConfig, which
 * reads them from local.properties at build time.
 */
class BlocklistSyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        // Misconfigured (no endpoint baked in) — fail fast instead of retrying forever.
        if (BuildConfig.BLOCKLIST_URL.isBlank()) return Result.failure()
        return try {
            val conn = (URL(BuildConfig.BLOCKLIST_URL).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer ${BuildConfig.BLOCKLIST_SECRET}")
                connectTimeout = 15_000
                readTimeout = 15_000
            }
            if (conn.responseCode != 200) return Result.retry()
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            val numbers = JSONObject(body).getJSONArray("numbers")
            val set = HashSet<String>(numbers.length())
            for (i in 0 until numbers.length()) set.add(numbers.getString(i))
            BlocklistStore(applicationContext).replaceAll(set)
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        private const val UNIQUE = "blocklist-sync"

        private fun connectedConstraints() =
            Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun enqueuePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<BlocklistSyncWorker>(1, TimeUnit.HOURS)
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(UNIQUE, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun syncNow(context: Context): UUID {
            val request = OneTimeWorkRequestBuilder<BlocklistSyncWorker>()
                .setConstraints(connectedConstraints())
                .build()
            WorkManager.getInstance(context).enqueue(request)
            return request.id
        }
    }
}
