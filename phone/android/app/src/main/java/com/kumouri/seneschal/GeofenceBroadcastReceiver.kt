package com.kumouri.seneschal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent
import org.json.JSONObject
import java.util.TimeZone

/**
 * Turns OS geofence enter/exit callbacks into presence events, buffered for [PresenceSyncWorker] to POST.
 * Only the place *name* + transition are recorded — never coordinates. Wire shape matches the desktop's
 * `presence_import` (t=geofence).
 */
class GeofenceBroadcastReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return
        if (event.hasError()) return
        val transition = when (event.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> "enter"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "exit"
            else -> return
        }
        val nowMs = System.currentTimeMillis()
        val offsetMin = TimeZone.getDefault().getOffset(nowMs) / 60_000
        val store = PresenceStore(context)
        for (gf in event.triggeringGeofences.orEmpty()) {
            store.append(
                JSONObject()
                    .put("t", "geofence")
                    .put("place", gf.requestId)
                    .put("transition", transition)
                    .put("ts_ms", nowMs)
                    .put("offset_min", offsetMin)
                    .toString()
            )
        }
        PresenceSyncWorker.syncNow(context)
    }
}
