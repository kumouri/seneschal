package com.kumouri.seneschal

import android.content.Context
import org.json.JSONArray

/**
 * A small on-device buffer of pending presence-event NDJSON lines. The geofence receiver appends;
 * [PresenceSyncWorker] drains and POSTs them, clearing only what it successfully sent (so a failed POST
 * keeps the events for the next run).
 */
class PresenceStore(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @Synchronized
    fun append(line: String) {
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        arr.put(line)
        prefs.edit().putString(KEY, arr.toString()).apply()
    }

    @Synchronized
    fun drain(): List<String> {
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        return (0 until arr.length()).map { arr.getString(it) }
    }

    /**
     * Drop the first [count] buffered lines — the ones a successful POST just sent. Anything appended
     * since (indices >= count) is preserved, so a concurrent geofence event isn't lost.
     */
    @Synchronized
    fun clearFirst(count: Int) {
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        val remaining = JSONArray()
        for (i in count until arr.length()) remaining.put(arr.getString(i))
        prefs.edit().putString(KEY, remaining.toString()).apply()
    }

    private companion object {
        const val PREFS = "presence"
        const val KEY = "pending_events"
    }
}
