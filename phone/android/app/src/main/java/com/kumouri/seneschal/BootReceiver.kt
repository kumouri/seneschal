package com.kumouri.seneschal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Geofences are dropped on reboot; re-register them (and re-enqueue the flush worker) once the device
 * finishes booting, so presence keeps flowing without the owner reopening the app.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            Geofencing.registerAll(context)
            ActivityRecognitionUpdates.register(context)
            SleepUpdates.register(context)
            PresenceSyncWorker.enqueuePeriodic(context)
        }
    }
}
