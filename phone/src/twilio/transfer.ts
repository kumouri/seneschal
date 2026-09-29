/**
 * Live transfer: ring the owner's cell up to LIVE_TRANSFER_ATTEMPTS times, then
 * take a voicemail — a caller the screener decided to put through gets a few
 * real chances to reach the owner before being asked to leave a message.
 *
 * Every `<Dial>` that bridges a caller to the owner carries an `action` URL of
 * `${base}/after-bridge?attempt=N&from=…`; Twilio POSTs the DialCallStatus there
 * when the dial ends and `nextLiveTransferStep` (pure — unit-tested) decides:
 * answered ⇒ hang up, unanswered and attempts remain ⇒ dial again with N+1,
 * otherwise ⇒ voicemail.
 *
 * NOT the reminder escalation's cap. `src/escalation/escalation.ts`'s
 * DEFAULT_MAX_ATTEMPTS (the assistant calling the owner until a digit is pressed)
 * is a different loop with a deliberately higher number and is untouched by this:
 * a caller shouldn't be held for minutes, but a can't-miss reminder can keep
 * trying for half an hour.
 */
import type { Env } from "../config";
import { dial } from "../twiml";

/** How many times a live transfer rings the owner before rolling to voicemail. */
export const LIVE_TRANSFER_ATTEMPTS = 3;

/**
 * Ring time per attempt. Short so the caller isn't held for minutes across three
 * tries, and — the load-bearing part — BELOW the owner's cell no-answer-forward
 * timer, so a missed bridge can't be forwarded back into Twilio and screened
 * again (the caller would hear the screener pick up a second time, and the owner's
 * phone would ring over and over). Carrier forward timers are commonly 15–30 s and
 * often user-shortened; 12 s leaves margin under a 15 s forward. If the owner's
 * forward is shorter still, lower this with it — never assume a long forward.
 */
export const LIVE_TRANSFER_RING_SEC = 12;

export type LiveTransferStep = { kind: "hangup" } | { kind: "redial"; attempt: number } | { kind: "voicemail" };

/**
 * `attempt` is the 1-based number of the dial that just ended; `status` is
 * Twilio's DialCallStatus for it ("completed" = the owner answered).
 */
export function nextLiveTransferStep(status: string, attempt: number): LiveTransferStep {
  if (status === "completed") return { kind: "hangup" };
  const n = Number.isFinite(attempt) && attempt >= 1 ? Math.floor(attempt) : 1;
  if (n < LIVE_TRANSFER_ATTEMPTS) return { kind: "redial", attempt: n + 1 };
  return { kind: "voicemail" };
}

/** Parse the `attempt` query param of an /after-bridge callback; anything odd is attempt 1. */
export function parseAttempt(raw: string | null): number {
  const n = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(n) && n >= 1 ? n : 1;
}

/** The /after-bridge action URL for a given attempt, carrying the caller for the voicemail leg. */
export function afterBridgeUrl(base: string, attempt: number, fromE164: string): string {
  const q = new URLSearchParams({ attempt: String(attempt) });
  if (fromE164 !== "") q.set("from", fromE164);
  return `${base}/after-bridge?${q.toString()}`;
}

/** TwiML for attempt N of a live transfer to the owner's cell. */
export function liveTransferTwiml(env: Env, base: string, attempt: number, fromE164: string): string {
  return dial(env.USER_CELL_E164, env.TWILIO_NUMBER_E164, {
    timeoutSec: LIVE_TRANSFER_RING_SEC,
    actionUrl: afterBridgeUrl(base, attempt, fromE164),
  });
}
