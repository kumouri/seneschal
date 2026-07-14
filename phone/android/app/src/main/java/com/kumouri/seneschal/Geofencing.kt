package com.kumouri.seneschal

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices

/**
 * (Re)registers the saved places as OS geofences. Enter/exit then fire [GeofenceBroadcastReceiver] even
 * when the app isn't running — that's what the "all the time" background-location grant buys. Coordinates
 * are read from the on-device [PlacesStore] and handed straight to the OS; nothing here leaves the phone.
 */
object Geofencing {

    fun hasFineLocation(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, GeofenceBroadcastReceiver::class.java)
            .setAction(ACTION)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags = flags or PendingIntent.FLAG_MUTABLE
        return PendingIntent.getBroadcast(context, 0, intent, flags)
    }

    /** (Re)register every saved place. No-op without fine-location permission; safe to call repeatedly. */
    fun registerAll(context: Context) {
        if (!hasFineLocation(context)) return
        val client = LocationServices.getGeofencingClient(context)
        client.removeGeofences(pendingIntent(context))
        val places = PlacesStore(context).all()
        if (places.isEmpty()) return
        val geofences = places.map { p ->
            Geofence.Builder()
                .setRequestId(p.name)
                .setCircularRegion(p.lat, p.lon, p.radiusM)
                .setExpirationDuration(Geofence.NEVER_EXPIRE)
                .setTransitionTypes(
                    Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT)
                .build()
        }
        val request = GeofencingRequest.Builder()
            .setInitialTrigger(GeofencingRequest.INITIAL_TRIGGER_ENTER)
            .addGeofences(geofences)
            .build()
        try {
            client.addGeofences(request, pendingIntent(context))
        } catch (_: SecurityException) {
            // Background location not granted yet; enter/exit just won't fire until it is.
        }
    }

    const val ACTION = "com.kumouri.seneschal.GEOFENCE"
}
