/**
 * Place an *outbound* call via Twilio's REST API. Unlike src/twilio/calls.ts
 * (which re-points a live inbound call), this originates a brand-new call that
 * speaks a short line and hangs up — the assistant ringing the owner for a
 * can't-miss reminder. Uses Twilio's inline `Twiml` parameter, so no public webhook/URL is
 * needed: the whole `<Say>` script travels in the create request. `fetch`-based
 * so it runs in the Workers runtime.
 *
 * ## The voice
 *
 * Both reminder shapes speak in the persona's TTS voice — `ttsProvider` + `voice`
 * from `personaFromEnv` (`ASSISTANT_TTS_PROVIDER` / `ASSISTANT_VOICE_ID`), the same
 * pair the inbound screener hands to ConversationRelay — via Twilio's
 * `<Say voice="<Provider>.<id>">`. Twilio's default voice is a *runtime fallback
 * only*, never the first choice when a persona voice is configured. Two layers
 * keep a reminder from failing because a voice did:
 *
 * 1. **Create-time.** If Twilio rejects the create request carrying the persona
 *    voice (any non-2xx — e.g. the account can't use a beta provider and the
 *    inline TwiML fails validation), the same call is re-placed once with a bare
 *    `<Say>`. A non-2xx create means no call was placed, so the retry cannot
 *    double-ring.
 * 2. **In-call.** An invalid `voice` value at speak time is Twilio warning 13511
 *    ("Say: Invalid voice value", log level WARNING), which does not terminate
 *    the call — Twilio speaks with its default voice and logs the warning. That
 *    layer is Twilio's, not ours; `wrangler tail` / the Twilio debugger is where
 *    a persistently-defaulting voice shows up.
 *
 * With no persona voice configured (the shipped default) every call is exactly
 * one plain-`<Say>` request.
 */
import type { Env } from "../config";
import { personaFromEnv } from "../config";
import { escalationGather, say, sayVoiceOf } from "../twiml";

/** Twilio REST resource for creating a new call on an account (pure — unit-tested). */
export function callsCreateUrl(accountSid: string): string {
  return `https://api.twilio.com/2010-04-01/Accounts/${accountSid}/Calls.json`;
}

/** One Twilio call-create POST with inline TwiML. Throws on a non-2xx response. */
async function createCall(env: Env, toE164: string, twiml: string): Promise<string> {
  const res = await fetch(callsCreateUrl(env.TWILIO_ACCOUNT_SID), {
    method: "POST",
    headers: {
      Authorization: `Basic ${btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`)}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      To: toE164,
      From: env.TWILIO_NUMBER_E164,
      Twiml: twiml,
    }),
  });
  if (!res.ok) {
    throw new Error(`Twilio call create ${res.status}: ${await res.text()}`);
  }
  const data = (await res.json()) as { sid?: string };
  return data.sid ?? "";
}

/**
 * Place a call in the persona's voice; if Twilio refuses that create, place it
 * once more in the default voice. `build(voice)` renders the TwiML for a given
 * `<Say voice>` (undefined => plain `<Say>`). The fallback is skipped entirely
 * when the persona names no voice, so a voiceless persona is exactly one request.
 */
async function createCallInPersonaVoice(
  env: Env,
  toE164: string,
  build: (voice: string | undefined) => string,
): Promise<string> {
  const voice = sayVoiceOf(personaFromEnv(env));
  if (voice === undefined) return createCall(env, toE164, build(undefined));
  try {
    return await createCall(env, toE164, build(voice));
  } catch (e) {
    // A reminder call never fails because a voice did: re-place it in Twilio's
    // default voice. If THAT fails too, the error is the real one and propagates.
    console.log(`push-call: persona voice ${voice} rejected at create, falling back to default voice: ${String(e)}`);
    return createCall(env, toE164, build(undefined));
  }
}

/**
 * Ring `toE164` from the Twilio number, speak `message`, and hang up. Returns the
 * new call SID. Inline TwiML (`<Say>…</Say><Hangup />`, ≤4000 chars) keeps this
 * self-contained; for a spoken reminder that's all we need (no ConversationRelay).
 */
export async function placeCall(env: Env, toE164: string, message: string): Promise<string> {
  return createCallInPersonaVoice(env, toE164, (voice) => say(message, { hangup: true, voice }));
}

/**
 * Place one *escalating* reminder call: speak `message`, then listen for a keypress
 * (via `<Gather action=ackUrl>`). If the owner presses a digit, Twilio POSTs `ackUrl` and
 * the CallEscalation Durable Object stops retrying; otherwise the DO's alarm calls
 * back on a cadence. Returns the new call SID. Sibling of `placeCall` (single ring).
 */
export async function placeEscalationCall(
  env: Env,
  toE164: string,
  message: string,
  ackUrl: string,
): Promise<string> {
  const prompt = `${message} — press 1 to let me know you got it, and I'll stop calling.`;
  return createCallInPersonaVoice(env, toE164, (voice) => escalationGather({ prompt, actionUrl: ackUrl, voice }));
}
