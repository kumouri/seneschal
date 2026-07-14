package com.kumouri.seneschal

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import com.google.android.gms.location.ActivityRecognition
import com.google.android.gms.location.SleepSegmentRequest

/**
 * Subscribes to the Android **Sleep API** — periodic sleep-confidence classifications + post-hoc sleep
 * segments. The signal is **informational only**: the desktop reminder gate retired its asleep rule
 * (2026-07-13) because the phone-only Sleep API reads an idle phone as a sleeping owner — a
 * wearable-grade signal (watch HR + wrist motion) is the prerequisite to gate on sleep again. The Health
 * Connect circadian feed stays the authority on *biological* morning. Needs `ACTIVITY_RECOGNITION`
 * (already requested for the driving gate).
 */
object SleepUpdates {

    const val ACTION = "com.kumouri.seneschal.SLEEP"

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, SleepReceiver::class.java).setAction(ACTION)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags = flags or PendingIntent.FLAG_MUTABLE
        return PendingIntent.getBroadcast(context, 0, intent, flags)
    }

    /** (Re)subscribe to sleep classify + segment updates. No-op without ACTIVITY_RECOGNITION. */
    fun register(context: Context) {
        if (!ActivityRecognitionUpdates.hasPermission(context)) return
        try {
            ActivityRecognition.getClient(context).requestSleepSegmentUpdates(
                pendingIntent(context), SleepSegmentRequest.getDefaultSleepSegmentRequest())
        } catch (_: SecurityException) {
            // permission not granted yet; sleep events just won't fire until it is.
        }
    }
}
