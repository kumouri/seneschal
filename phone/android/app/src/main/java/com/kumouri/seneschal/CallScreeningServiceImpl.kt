package com.kumouri.seneschal

import android.telecom.Call
import android.telecom.CallScreeningService

/**
 * Silences incoming calls whose number is on the synced blocklist — before
 * the phone ever rings. Everything else is allowed through untouched (contacts,
 * unknowns), so the cloud screener still handles the unknowns you miss.
 *
 * Block action = silent reject: the call is declined, you get no ring and no
 * missed-call notification, but it stays in the call log so you can see what was
 * blocked.
 */
class CallScreeningServiceImpl : CallScreeningService() {

    override fun onScreenCall(callDetails: Call.Details) {
        val incoming = callDetails.handle?.schemeSpecificPart
        val number = normalizeToE164(incoming)
        val blocked = number != null && BlocklistStore(this).contains(number)

        val response = CallResponse.Builder()
            .setDisallowCall(blocked)
            .setRejectCall(blocked)
            .setSkipCallLog(false)        // keep blocked calls visible in the log
            .setSkipNotification(blocked) // ...but no missed-call notification
            .build()

        respondToCall(callDetails, response)
    }

    /** Best-effort match to the +1XXXXXXXXXX E.164 form the blocklist stores. */
    private fun normalizeToE164(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        val cleaned = raw.filter { it.isDigit() || it == '+' }
        val digits = cleaned.filter { it.isDigit() }
        return when {
            cleaned.startsWith("+") -> "+$digits"
            digits.length == 10 -> "+1$digits"
            digits.length == 11 && digits.startsWith("1") -> "+$digits"
            else -> null
        }
    }
}
