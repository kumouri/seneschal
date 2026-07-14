package com.kumouri.seneschal

import android.content.Context

/**
 * Local cache of the screener's blocklist, kept in SharedPreferences (no DB dependency).
 * The screening service reads it on every call; the sync worker replaces it.
 */
class BlocklistStore(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun contains(numberE164: String): Boolean =
        prefs.getStringSet(KEY_NUMBERS, emptySet())?.contains(numberE164) == true

    fun replaceAll(numbers: Set<String>) {
        prefs.edit()
            .putStringSet(KEY_NUMBERS, numbers)
            .putLong(KEY_SYNCED_AT, System.currentTimeMillis())
            .apply()
    }

    fun count(): Int = prefs.getStringSet(KEY_NUMBERS, emptySet())?.size ?: 0

    /** Epoch millis of the last successful sync, or 0 if never. */
    fun lastSyncedAt(): Long = prefs.getLong(KEY_SYNCED_AT, 0L)

    private companion object {
        const val PREFS = "seneschal"
        const val KEY_NUMBERS = "blocklist_numbers"
        const val KEY_SYNCED_AT = "blocklist_synced_at"
    }
}
