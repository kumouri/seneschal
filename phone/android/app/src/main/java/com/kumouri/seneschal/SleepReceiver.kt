package com.kumouri.seneschal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.google.android.gms.location.SleepClassifyEvent
import com.google.android.gms.location.SleepSegmentEvent
import org.json.JSONObject
import java.util.TimeZone

/**
 * Turns Android Sleep API callbacks into sleep/wake presence events (t=sleep). A high-confidence classify
 * => `asleep`; a completed sleep segment (delivered once the owner is up) => `awake`. Only the state + timestamp
 * is recorded. The desktop's `presence_import` folds these into `presence-context.json.asleep`, which is
 * **informational only** — the reminder gate no longer consumes it (see SleepUpdates).
 */
class SleepReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val store = PresenceStore(context)
        var any = false
        if (SleepClassifyEvent.hasEvents(intent)) {
            SleepClassifyEvent.extractEvents(intent).lastOrNull()?.let { e ->
                val state = if (e.confidence >= SLEEP_CONFIDENCE) "asleep" else "awake"
                store.append(sleepJson(state, e.timestampMillis, e.confidence))
                any = true
            }
        }
        if (SleepSegmentEvent.hasEvents(intent)) {
            // A completed sleep segment is delivered once the owner is up — a wake edge.
            SleepSegmentEvent.extractEvents(intent).lastOrNull()?.let { e ->
                store.append(sleepJson("awake", e.endTimeMillis, null))
                any = true
            }
        }
        if (any) PresenceSyncWorker.syncNow(context)
    }

    private fun sleepJson(state: String, tsMs: Long, confidence: Int?): String {
        val o = JSONObject()
            .put("t", "sleep")
            .put("state", state)
            .put("ts_ms", tsMs)
            .put("offset_min", TimeZone.getDefault().getOffset(tsMs) / 60_000)
        if (confidence != null) o.put("confidence", confidence)
        return o.toString()
    }

    private companion object {
        /** Sleep API confidence (0-100) at/above which we call it asleep. */
        const val SLEEP_CONFIDENCE = 70
    }
}
