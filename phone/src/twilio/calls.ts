/**
 * Twilio call-control via the REST API: redirect a *live* call out of
 * ConversationRelay by replacing its TwiML. Used at stage-4 to bridge the
 * caller to the owner (connect) or to speak a closing line and hang up
 * (message / spam). `fetch`-based so it runs in the Workers runtime.
 */
import type { Env } from "../config";
import { say } from "../twiml";
import { liveTransferTwiml } from "./transfer";

/** The Twilio REST resource for a single live call (pure — unit-tested). */
export function callResourceUrl(accountSid: string, callSid: string): string {
  return `https://api.twilio.com/2010-04-01/Accounts/${accountSid}/Calls/${callSid}.json`;
}

async function updateCallTwiml(env: Env, callSid: string, twiml: string): Promise<void> {
  const res = await fetch(callResourceUrl(env.TWILIO_ACCOUNT_SID, callSid), {
    method: "POST",
    headers: {
      Authorization: `Basic ${btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`)}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ Twiml: twiml }),
  });
  if (!res.ok) {
    throw new Error(`Twilio call update ${res.status}: ${await res.text()}`);
  }
}

/**
 * Bridge the live call to the owner's cell (ends ConversationRelay). This is
 * attempt 1 of the live transfer: when the dial ends, Twilio POSTs the result to
 * `${baseUrl}/after-bridge?attempt=1`, which hangs up if the owner answered,
 * re-dials up to LIVE_TRANSFER_ATTEMPTS, and only then rolls to voicemail
 * (`src/twilio/transfer.ts` — also where the ring time lives, kept below the
 * cell's no-answer-forward timer). `fromE164` rides the action URL so the
 * voicemail leg knows who called.
 */
export async function redirectToDial(env: Env, callSid: string, baseUrl: string, fromE164: string): Promise<void> {
  await updateCallTwiml(env, callSid, liveTransferTwiml(env, baseUrl, 1, fromE164));
}

/** Speak a closing line and hang up the live call. */
export async function redirectToHangup(env: Env, callSid: string, message: string): Promise<void> {
  await updateCallTwiml(env, callSid, say(message, { hangup: true }));
}
