/**
 * Twilio call-control via the REST API: redirect a *live* call out of
 * ConversationRelay by replacing its TwiML. Used at stage-4 to bridge the
 * caller to the owner (connect) or to speak a closing line and hang up
 * (message / spam). `fetch`-based so it runs in the Workers runtime.
 */
import type { Env } from "../config";
import { dial, say } from "../twiml";

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
 * Bridge the live call to the owner's cell (ends ConversationRelay). The 18s
 * ring window is deliberately shorter than the cell's no-answer-forward timer
 * (~25s) so a missed bridge times out here instead of looping back through the
 * forward. When the dial ends, Twilio POSTs the result to `${baseUrl}/after-bridge`,
 * which connects-or-voicemails based on whether the owner answered.
 */
export async function redirectToDial(env: Env, callSid: string, toE164: string, baseUrl: string): Promise<void> {
  await updateCallTwiml(
    env,
    callSid,
    dial(toE164, env.TWILIO_NUMBER_E164, {
      timeoutSec: 18,
      actionUrl: `${baseUrl}/after-bridge`,
    }),
  );
}

/** Speak a closing line and hang up the live call. */
export async function redirectToHangup(env: Env, callSid: string, message: string): Promise<void> {
  await updateCallTwiml(env, callSid, say(message, { hangup: true }));
}
