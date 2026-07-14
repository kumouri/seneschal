package com.kumouri.seneschal

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import com.google.android.gms.location.ActivityRecognition
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionRequest
import com.google.android.gms.location.DetectedActivity

/**
 * Subscribes to OS Activity Recognition **transitions** (still / walking / running / in-vehicle /
 * on-bicycle) so the daemon can hold non-urgent nudges while the owner is driving, and release them once
 * they stop. Fires [ActivityTransitionReceiver] even when the app isn't running. Needs the
 * `ACTIVITY_RECOGNITION` runtime permission (API 29+). Only the activity label + enter/exit is recorded —
 * no location, no raw sensor data.
 */
object ActivityRecognitionUpdates {

    const val ACTION = "com.kumouri.seneschal.ACTIVITY_TRANSITION"

    private val ACTIVITIES = listOf(
        DetectedActivity.STILL,
        DetectedActivity.WALKING,
        DetectedActivity.RUNNING,
        DetectedActivity.IN_VEHICLE,
        DetectedActivity.ON_BICYCLE,
    )

    fun hasPermission(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.ACTIVITY_RECOGNITION) ==
                PackageManager.PERMISSION_GRANTED

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, ActivityTransitionReceiver::class.java).setAction(ACTION)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags = flags or PendingIntent.FLAG_MUTABLE
        return PendingIntent.getBroadcast(context, 0, intent, flags)
    }

    /** (Re)subscribe to enter+exit transitions for the tracked activities. No-op without permission. */
    fun register(context: Context) {
        if (!hasPermission(context)) return
        val transitions = ArrayList<ActivityTransition>()
        for (act in ACTIVITIES) {
            for (tt in intArrayOf(
                ActivityTransition.ACTIVITY_TRANSITION_ENTER,
                ActivityTransition.ACTIVITY_TRANSITION_EXIT
            )) {
                val builder = ActivityTransition.Builder()
                builder.setActivityType(act)
                builder.setActivityTransition(tt)
                transitions.add(builder.build())
            }
        }
        try {
            ActivityRecognition.getClient(context)
                .requestActivityTransitionUpdates(ActivityTransitionRequest(transitions), pendingIntent(context))
        } catch (_: SecurityException) {
            // permission not granted yet; transitions just won't fire until it is.
        }
    }

    /** Map a DetectedActivity type to the wire label the desktop expects; null = ignore this activity. */
    fun label(activityType: Int): String? = when (activityType) {
        DetectedActivity.STILL -> "still"
        DetectedActivity.WALKING -> "walking"
        DetectedActivity.RUNNING -> "running"
        DetectedActivity.IN_VEHICLE -> "in_vehicle"
        DetectedActivity.ON_BICYCLE -> "on_bicycle"
        else -> null
    }
}
