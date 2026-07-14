package com.kumouri.seneschal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionResult
import org.json.JSONObject
import java.util.TimeZone

/**
 * Turns OS activity-transition callbacks (still / walking / driving …) into presence events buffered for
 * [PresenceSyncWorker] to POST. Only the activity label + enter/exit is recorded. Wire shape matches the
 * desktop's `presence_import` (t=activity).
 */
class ActivityTransitionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (!ActivityTransitionResult.hasResult(intent)) return
        val result = ActivityTransitionResult.extractResult(intent) ?: return
        val nowMs = System.currentTimeMillis()
        val offsetMin = TimeZone.getDefault().getOffset(nowMs) / 60_000
        val store = PresenceStore(context)
        var any = false
        for (event in result.transitionEvents) {
            val label = ActivityRecognitionUpdates.label(event.activityType) ?: continue
            val transition = when (event.transitionType) {
                ActivityTransition.ACTIVITY_TRANSITION_ENTER -> "enter"
                ActivityTransition.ACTIVITY_TRANSITION_EXIT -> "exit"
                else -> continue
            }
            store.append(
                JSONObject()
                    .put("t", "activity")
                    .put("activity", label)
                    .put("transition", transition)
                    .put("ts_ms", nowMs)
                    .put("offset_min", offsetMin)
                    .toString()
            )
            any = true
        }
        if (any) PresenceSyncWorker.syncNow(context)
    }
}
