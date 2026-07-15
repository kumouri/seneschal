package com.kumouri.seneschal

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * A named geofence region. Coordinates live only on the device (SharedPreferences) — never in git and
 * never on the wire. The presence feed sends only the place *name* + transition, so the desktop learns
 * "the owner arrived home", never where home is.
 */
data class Place(val name: String, val lat: Double, val lon: Double, val radiusM: Float)

/**
 * On-device store of the owner's named places (home, work, …). The in-app editor writes here; geofence
 * registration and the boot receiver read here.
 */
class PlacesStore(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun all(): List<Place> {
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Place(o.getString("name"), o.getDouble("lat"), o.getDouble("lon"),
                o.getDouble("radius_m").toFloat())
        }
    }

    /** Add or replace the place with this name (names are the geofence request ids, so they're unique). */
    fun save(place: Place) {
        writeAll(all().filter { it.name != place.name } + place)
    }

    fun remove(name: String) {
        writeAll(all().filter { it.name != name })
    }

    private fun writeAll(places: List<Place>) {
        val arr = JSONArray()
        for (p in places) {
            arr.put(JSONObject().put("name", p.name).put("lat", p.lat).put("lon", p.lon)
                .put("radius_m", p.radiusM.toDouble()))
        }
        prefs.edit().putString(KEY, arr.toString()).apply()
    }

    private companion object {
        const val PREFS = "places"
        const val KEY = "places"
    }
}
