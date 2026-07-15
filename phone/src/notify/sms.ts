/**
 * Notify the owner by SMS via Twilio's REST API. Used to deliver call verdicts
 * and message transcripts (wired in M3). Plain `fetch` so it runs unchanged in
 * the Workers runtime.
 */
import type { Env } from "../config";

export async function sendSms(env: Env, toE164: string, body: string): Promise<void> {
  const url = `https://api.twilio.com/2010-04-01/Accounts/${env.TWILIO_ACCOUNT_SID}/Messages.json`;
  const auth = btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`);
  const form = new URLSearchParams({ To: toE164, From: env.TWILIO_NUMBER_E164, Body: body });

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form,
  });

  if (!res.ok) {
    throw new Error(`Twilio SMS failed: ${res.status} ${await res.text()}`);
  }
}
