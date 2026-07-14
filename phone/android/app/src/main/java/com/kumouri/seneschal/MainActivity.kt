package com.kumouri.seneschal

import android.Manifest
import android.app.role.RoleManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import java.text.DateFormat
import java.util.Date

/**
 * One-screen control panel: grant the Call-Screening role, see how many numbers
 * are blocked + when we last synced, and force a sync. The actual blocking runs
 * in CallScreeningServiceImpl; blocklist syncing in BlocklistSyncWorker; the
 * Health Connect -> desktop feed in HealthSyncWorker.
 */
class MainActivity : AppCompatActivity() {

    private val roleRequest =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { refresh() }

    // Health Connect grants permissions through its own system UI; this contract launches it.
    private val healthPermsRequest =
        registerForActivityResult(PermissionController.createRequestPermissionResultContract()) {
            HealthSyncWorker.syncNow(this)  // first pull as soon as reads are granted
        }

    // Location for the presence feed: request FINE in-app, then send the owner to Settings for background.
    private val locationPermsRequest =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { grants ->
            if (grants[Manifest.permission.ACCESS_FINE_LOCATION] == true) {
                Geofencing.registerAll(this)
                maybePromptBackgroundLocation()
            }
        }

    // Activity Recognition (still / walking / driving) for the presence feed's driving gate.
    private val activityPermRequest =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                ActivityRecognitionUpdates.register(this)
                SleepUpdates.register(this)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        findViewById<Button>(R.id.grantRole).setOnClickListener { requestScreeningRole() }
        findViewById<Button>(R.id.syncNow).setOnClickListener {
            val blocklistId = BlocklistSyncWorker.syncNow(this)
            HealthSyncWorker.syncNow(this)
            findViewById<TextView>(R.id.status).text = getString(R.string.syncing)
            // Reflect the result instead of sitting on "Syncing…" forever.
            WorkManager.getInstance(this).getWorkInfoByIdLiveData(blocklistId)
                .observe(this) { info -> if (info != null && info.state.isFinished) refresh() }
        }

        findViewById<Button>(R.id.addPlace).setOnClickListener { addPlaceHere() }
        findViewById<Button>(R.id.managePlaces).setOnClickListener { managePlaces() }

        // Keep the blocklist fresh and the health + presence feeds flowing in the background.
        BlocklistSyncWorker.enqueuePeriodic(this)
        HealthSyncWorker.enqueuePeriodic(this)
        PresenceSyncWorker.enqueuePeriodic(this)
        Geofencing.registerAll(this)  // re-register saved geofences on open, if permission allows
        ActivityRecognitionUpdates.register(this)  // re-subscribe activity transitions if permitted
        SleepUpdates.register(this)                // ...and sleep/wake (same permission)
        maybeRequestActivityRecognition()
    }

    override fun onResume() {
        super.onResume()
        refresh()
        maybeRequestHealthPermissions()
    }

    /** Ask for Health Connect read access if it's available and not yet granted. */
    private fun maybeRequestHealthPermissions() {
        if (HealthConnectClient.getSdkStatus(this) != HealthConnectClient.SDK_AVAILABLE) return
        val client = HealthConnectClient.getOrCreate(this)
        lifecycleScope.launchWhenResumed {
            val granted = client.permissionController.getGrantedPermissions()
            if (!granted.containsAll(HealthSyncWorker.READ_PERMISSIONS)) {
                healthPermsRequest.launch(HealthSyncWorker.READ_PERMISSIONS)
            }
        }
    }

    private fun requestScreeningRole() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        val rm = getSystemService(RoleManager::class.java) ?: return
        if (rm.isRoleAvailable(RoleManager.ROLE_CALL_SCREENING) &&
            !rm.isRoleHeld(RoleManager.ROLE_CALL_SCREENING)
        ) {
            roleRequest.launch(rm.createRequestRoleIntent(RoleManager.ROLE_CALL_SCREENING))
        }
    }

    private fun isRoleHeld(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
        val rm = getSystemService(RoleManager::class.java) ?: return false
        return rm.isRoleHeld(RoleManager.ROLE_CALL_SCREENING)
    }

    private fun refresh() {
        val store = BlocklistStore(this)
        val held = isRoleHeld()
        val syncedAt = store.lastSyncedAt()
        val syncedText =
            if (syncedAt == 0L) "never" else DateFormat.getDateTimeInstance().format(Date(syncedAt))

        val places = PlacesStore(this).all()
        findViewById<TextView>(R.id.status).text = buildString {
            appendLine(if (held) "✅ Screening active" else "⚠️ Tap below to make Seneschal the screening app")
            appendLine("Blocked numbers: ${store.count()}")
            appendLine("Last synced: $syncedText")
            append(if (places.isEmpty()) "Places: none yet" else "Places: " + places.joinToString(", ") { it.name })
        }
        findViewById<Button>(R.id.grantRole).isEnabled = !held
    }

    // --- Presence: capture a place here, register its geofence ---

    private fun addPlaceHere() {
        if (!Geofencing.hasFineLocation(this)) {
            locationPermsRequest.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION))
            return
        }
        val cts = CancellationTokenSource()
        try {
            LocationServices.getFusedLocationProviderClient(this)
                .getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, cts.token)
                .addOnSuccessListener { loc ->
                    if (loc == null) toast("Couldn't get a fix — try again, ideally near a window.")
                    else promptSavePlace(loc.latitude, loc.longitude)
                }
                .addOnFailureListener { toast("Location error: ${it.message}") }
        } catch (_: SecurityException) {
            locationPermsRequest.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION))
        }
    }

    private fun promptSavePlace(lat: Double, lon: Double) {
        val nameInput = EditText(this).apply {
            hint = "Place name (e.g. home)"
            setText("home")
        }
        val radiusInput = EditText(this).apply {
            hint = "Radius in metres"
            inputType = InputType.TYPE_CLASS_NUMBER
            setText("150")
        }
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(nameInput)
            addView(radiusInput)
        }
        AlertDialog.Builder(this)
            .setTitle("Save this location")
            .setMessage("Lat %.5f, Lon %.5f".format(lat, lon))
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                val name = nameInput.text.toString().trim().ifEmpty { "home" }
                val radius = radiusInput.text.toString().toFloatOrNull()?.coerceIn(50f, 2000f) ?: 150f
                PlacesStore(this).save(Place(name, lat, lon, radius))
                Geofencing.registerAll(this)
                toast("Saved “$name” (${radius.toInt()} m) and registered its geofence.")
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // --- Presence: view / delete saved places ---

    private fun managePlaces() {
        val places = PlacesStore(this).all()
        if (places.isEmpty()) {
            toast("No places yet — tap “Save this spot as a place” first.")
            return
        }
        val labels = places.map { "${it.name}  (${it.radiusM.toInt()} m)" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Places")
            .setItems(labels) { _, i -> confirmDeletePlace(places[i]) }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun confirmDeletePlace(place: Place) {
        AlertDialog.Builder(this)
            .setTitle("Delete “${place.name}”?")
            .setMessage("Lat %.5f, Lon %.5f — radius %d m".format(place.lat, place.lon, place.radiusM.toInt()))
            .setPositiveButton("Delete") { _, _ ->
                PlacesStore(this).remove(place.name)
                Geofencing.registerAll(this)
                toast("Deleted “${place.name}.”")
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun maybePromptBackgroundLocation() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            == PackageManager.PERMISSION_GRANTED
        ) return
        AlertDialog.Builder(this)
            .setTitle("Allow location “all the time”")
            .setMessage(
                "For geofences to fire while the app is closed, Android needs background location. " +
                    "Tap Open, then choose “Allow all the time.”"
            )
            .setPositiveButton("Open") { _, _ ->
                startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null)
                    )
                )
            }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun maybeRequestActivityRecognition() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        if (!ActivityRecognitionUpdates.hasPermission(this)) {
            activityPermRequest.launch(Manifest.permission.ACTIVITY_RECOGNITION)
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
