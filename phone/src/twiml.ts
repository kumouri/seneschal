/**
 * Tiny, dependency-free TwiML builders. Twilio expects an XML document in the
 * webhook response; these return the full string (with XML declaration) for
 * each action the funnel can take. Pure functions => unit-testable.
 */

const XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>';

/** Escape a string for safe inclusion in XML text / attribute values. */
export function escapeXml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function doc(body: string): string {
  return `${XML_DECL}<Response>${body}</Response>`;
}

export interface DialOptions {
  /** Ring time before giving up. Keep this BELOW the cell's no-answer-forward
   *  timer so a missed bridge can't be re-forwarded into a loop. */
  timeoutSec?: number;
  /** Spoken to the caller if the dial isn't answered in time, then hang up. */
  fallbackMessage?: string;
  /** Twilio POSTs the DialCallStatus here when the dial ends (answered or not). */
  actionUrl?: string;
}

/** Bridge the call to a number (used for allowlisted contacts and live transfer). */
export function dial(toE164: string, callerIdE164: string, opts: DialOptions = {}): string {
  const timeout = opts.timeoutSec !== undefined ? ` timeout="${opts.timeoutSec}"` : "";
  const action = opts.actionUrl !== undefined ? ` action="${escapeXml(opts.actionUrl)}" method="POST"` : "";
  const fallback = opts.fallbackMessage !== undefined ? `<Say>${escapeXml(opts.fallbackMessage)}</Say><Hangup />` : "";
  return doc(`<Dial${timeout}${action} callerId="${escapeXml(callerIdE164)}">${escapeXml(toE164)}</Dial>${fallback}`);
}

/** Bare hangup (used after a bridged call that already completed). */
export function hangupResponse(): string {
  return doc(`<Hangup />`);
}

export interface VoicemailOptions {
  prompt: string;
  /** Twilio POSTs the transcription (TranscriptionText/RecordingUrl) here when ready. */
  transcribeCallbackUrl: string;
  maxLengthSec?: number;
}

/** Prompt the caller, then record + transcribe a voicemail. */
export function voicemail(opts: VoicemailOptions): string {
  const maxLength = opts.maxLengthSec ?? 120;
  return doc(
    `<Say>${escapeXml(opts.prompt)}</Say>` +
      `<Record maxLength="${maxLength}" playBeep="true" transcribe="true" transcribeCallback="${escapeXml(opts.transcribeCallbackUrl)}" />`,
  );
}

/** Decline the call *without answering it* — Twilio does not bill rejected calls. */
export function reject(reason: "rejected" | "busy" = "rejected"): string {
  return doc(`<Reject reason="${reason}" />`);
}

/** Speak a message, optionally hanging up afterward. */
export function say(message: string, opts: { hangup?: boolean } = {}): string {
  return doc(`<Say>${escapeXml(message)}</Say>${opts.hangup ? "<Hangup />" : ""}`);
}

export interface GateOptions {
  prompt: string;
  /** Absolute URL Twilio POSTs the pressed digit to (our /gate route). */
  actionUrl: string;
  numDigits?: number;
  timeoutSec?: number;
}

/**
 * Press-1 gate. If the caller enters nothing within `timeoutSec`, `<Gather>`
 * falls through to `<Hangup>` — which is exactly how we drop silent robodialers
 * for ~$0. `actionOnEmptyResult="true"` makes that timeout ALSO POST to the
 * action URL (with empty Digits) first, so /gate can count the failure toward
 * the learning blocklist — without it, silent robodialers vanish unrecorded.
 */
export function gate(opts: GateOptions): string {
  const numDigits = opts.numDigits ?? 1;
  const timeout = opts.timeoutSec ?? 6;
  const gather =
    `<Gather numDigits="${numDigits}" timeout="${timeout}" action="${escapeXml(opts.actionUrl)}" method="POST" actionOnEmptyResult="true">` +
    `<Say>${escapeXml(opts.prompt)}</Say>` +
    `</Gather>`;
  return doc(`${gather}<Hangup />`);
}

export interface EscalationGatherOptions {
  /** Spoken line (the reminder + a "press 1" instruction). */
  prompt: string;
  /** Absolute URL Twilio POSTs the pressed digit to (our /push-call/ack route). */
  actionUrl: string;
  numDigits?: number;
  /** Seconds to wait for a keypress after the prompt before hanging up. */
  timeoutSec?: number;
}

/**
 * Escalating reminder call: speak the line and listen for a keypress. Same shape
 * as `gate()`, but a longer default timeout (the owner needs a moment to press) and the
 * `<Gather>` wraps the whole `<Say>` so a press *during* the message is captured.
 * On no input, falls through to `<Hangup>` — the Durable Object's alarm calls back.
 */
export function escalationGather(opts: EscalationGatherOptions): string {
  const numDigits = opts.numDigits ?? 1;
  const timeout = opts.timeoutSec ?? 30;
  const gather =
    `<Gather numDigits="${numDigits}" timeout="${timeout}" action="${escapeXml(opts.actionUrl)}" method="POST">` +
    `<Say>${escapeXml(opts.prompt)}</Say>` +
    `</Gather>`;
  return doc(`${gather}<Hangup />`);
}

export interface ConnectRelayOptions {
  /** wss:// URL of our ConversationRelay WebSocket (the Durable Object). */
  wsUrl: string;
  welcomeGreeting: string;
  /** Custom parameters surfaced to us in the ConversationRelay `setup` frame. */
  parameters?: Array<{ name: string; value: string }>;
  /** Persona TTS provider + voice (e.g. "Amazon" / "Joanna-Neural"). */
  ttsProvider?: string;
  voice?: string;
  /** Comma-separated STT vocabulary hints (e.g. the owner's name) to boost recognition. */
  hints?: string;
}

/** Hand the answered call to Twilio ConversationRelay (the paid Claude stage). */
export function connectRelay(opts: ConnectRelayOptions): string {
  const params = (opts.parameters ?? [])
    .map((p) => `<Parameter name="${escapeXml(p.name)}" value="${escapeXml(p.value)}" />`)
    .join("");
  const tts = opts.ttsProvider !== undefined ? ` ttsProvider="${escapeXml(opts.ttsProvider)}"` : "";
  const voice = opts.voice !== undefined ? ` voice="${escapeXml(opts.voice)}"` : "";
  const hints = opts.hints !== undefined ? ` hints="${escapeXml(opts.hints)}"` : "";
  return doc(
    `<Connect>` +
      `<ConversationRelay url="${escapeXml(opts.wsUrl)}" welcomeGreeting="${escapeXml(opts.welcomeGreeting)}" transcriptionProvider="Deepgram" speechModel="nova-3-general"${hints}${tts}${voice}>` +
      params +
      `</ConversationRelay>` +
      `</Connect>`,
  );
}
