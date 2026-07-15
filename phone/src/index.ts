/**
 * Worker entry / HTTP router. This is the tiered funnel in action:
 *   POST /voice — Twilio's inbound-call webhook. Runs the funnel and returns
 *                 TwiML: Dial (allow) / Reject (block) / Gather (press-1 gate).
 *   POST /gate  — the digit the caller pressed. "1" => ring through or hand to
 *                 ConversationRelay; anything else => hang up (robodialer).
 *   GET  /ws    — ConversationRelay WebSocket, handed to the RelaySession DO.
 *   GET  /status— liveness + effective settings (expanded into a dashboard in M5).
 *
 * The public base URL is taken from PUBLIC_BASE_URL when set, otherwise derived
 * from the incoming request — so it works behind a dev tunnel or a deployed
 * Worker without extra config.
 */
import type { Env } from "./config";
import { configured, personaFromEnv, settingsFromEnv } from "./config";
import type { CallerInfo } from "./screener/decision";
import { decideFunnel, decidePostGate } from "./screener/funnel";
import { listBlocklist, lookupLists, recordGateFail, syncGoogleContacts, type GoogleContact } from "./data/db";
import { connectRelay, dial, gate, hangupResponse, reject, say, voicemail } from "./twiml";
import { sendSms } from "./notify/sms";
import { placeCall } from "./notify/call";

export { RelaySession } from "./relay/session";
export { CallEscalation } from "./escalation/escalation";

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const { pathname } = new URL(request.url);
    const method = request.method;

    if (method === "POST" && pathname === "/voice") return handleVoice(request, env);
    if (method === "POST" && pathname === "/gate") return handleGate(request, env);
    if (pathname === "/ws") return handleWs(request, env);
    if (method === "GET" && pathname === "/status") return handleStatus(env);
    if (method === "GET" && pathname === "/blocklist") return handleBlocklist(request, env);
    if (method === "POST" && pathname === "/sync-contacts") return handleSyncContacts(request, env);
    if (method === "POST" && pathname === "/push-call") return handlePushCall(request, env);
    if (method === "POST" && pathname === "/push-call/ack") return handlePushCallAck(request, env);
    if (method === "POST" && pathname === "/after-bridge") return handleAfterBridge(request, env);
    if (method === "POST" && pathname === "/voicemail") return handleVoicemail(request, env);

    return new Response("not found", { status: 404 });
  },
};

function xml(body: string): Response {
  return new Response(body, { headers: { "Content-Type": "text/xml; charset=utf-8" } });
}

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v : "";
}

function baseUrlOf(request: Request, env: Env): string {
  const p = env.PUBLIC_BASE_URL;
  return p ? p : new URL(request.url).origin;
}

function wssOf(base: string): string {
  return base.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
}

async function handleVoice(request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  const from = field(form, "From");
  const to = field(form, "To");
  const caller: CallerInfo = { fromE164: from, toE164: to, hasCallerId: from !== "" };

  const settings = settingsFromEnv(env);
  const lists = await lookupLists(env.DB, from);
  const decision = decideFunnel(caller, lists, settings);

  switch (decision.stage) {
    case "allow":
      return xml(dial(settings.userCellE164, env.TWILIO_NUMBER_E164));
    case "reject":
      return xml(reject());
    default:
      // "gate" (and any fallthrough) -> cheap press-1 challenge.
      return xml(gate({ prompt: settings.gatePrompt, actionUrl: `${baseUrlOf(request, env)}/gate` }));
  }
}

async function handleGate(request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  const digits = field(form, "Digits");
  const from = field(form, "From");
  const to = field(form, "To");
  const settings = settingsFromEnv(env);

  if (digits !== "1") {
    // No / wrong key within the timeout: almost certainly a robodialer. Count
    // the failure toward the learning blocklist (rejected for $0 here, synced
    // to the phone app): silence blocks on the first strike, a wrong key gets
    // GATE_FAIL_BLOCK_AFTER strikes of grace (humans mash keys; robots don't).
    try {
      await recordGateFail(env.DB, from, to, digits === "");
    } catch (err) {
      // Learning is best-effort — never let a D1 hiccup break call handling.
      console.log(`recordGateFail error: ${err instanceof Error ? err.message : String(err)}`);
    }
    return xml(say("No input received. Goodbye.", { hangup: true }));
  }

  const post = decidePostGate(settings);
  if (post.stage === "allow") {
    return xml(dial(settings.userCellE164, env.TWILIO_NUMBER_E164));
  }

  // Hand to Claude. A per-call session id isolates this call's Durable Object
  // instance; the caller/callee numbers are passed through so the DO can log,
  // blocklist, and transfer without another lookup.
  const base = baseUrlOf(request, env);
  const sessionId = crypto.randomUUID();
  const persona = personaFromEnv(env);
  const owner = configured(env.OWNER_NAME);
  const ownerSpoken = configured(env.OWNER_NAME_SPOKEN) ?? owner;
  const hints = [...new Set([persona.name, owner, ownerSpoken])].filter((h): h is string => h !== undefined);
  return xml(
    connectRelay({
      wsUrl: `${wssOf(base)}/ws?s=${sessionId}`,
      welcomeGreeting: persona.greeting(ownerSpoken),
      ttsProvider: persona.ttsProvider,
      voice: persona.voice,
      hints: hints.length > 0 ? hints.join(", ") : undefined,
      parameters: [
        { name: "from", value: from },
        { name: "to", value: to },
        { name: "base", value: base },
      ],
    }),
  );
}

async function handleWs(request: Request, env: Env): Promise<Response> {
  if (request.headers.get("Upgrade") !== "websocket") {
    return new Response("expected websocket upgrade", { status: 426 });
  }
  const session = new URL(request.url).searchParams.get("s") ?? "active-call";
  const id = env.RELAY_SESSION.idFromName(session);
  return env.RELAY_SESSION.get(id).fetch(request);
}

function handleStatus(env: Env): Response {
  const settings = settingsFromEnv(env);
  const body = JSON.stringify({
    ok: true,
    postGateAction: settings.postGateAction,
    reputationLookupEnabled: settings.reputationLookupEnabled,
    dailyBudgetUsd: settings.dailyBudgetUsd,
  });
  return new Response(body, { headers: { "Content-Type": "application/json" } });
}

/** Serves the screener's learned blocklist to the on-device blocker app (bearer-authed). */
async function handleBlocklist(request: Request, env: Env): Promise<Response> {
  const secret = env.BLOCKLIST_SYNC_SECRET;
  if (secret === undefined || secret === "" || request.headers.get("Authorization") !== `Bearer ${secret}`) {
    return new Response("unauthorized", { status: 401 });
  }
  const numbers = await listBlocklist(env.DB);
  return new Response(JSON.stringify({ numbers, count: numbers.length }), {
    headers: { "Content-Type": "application/json" },
  });
}

/** Google Contacts sync push from the owner's Apps Script (see docs/contacts-sync.md). */
async function handleSyncContacts(request: Request, env: Env): Promise<Response> {
  const secret = env.CONTACTS_SYNC_SECRET;
  if (secret === undefined || secret === "" || request.headers.get("Authorization") !== `Bearer ${secret}`) {
    return new Response("unauthorized", { status: 401 });
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  const result = await syncGoogleContacts(env.DB, parseContacts(body));
  return new Response(JSON.stringify({ ok: true, ...result }), { headers: { "Content-Type": "application/json" } });
}

function parseContacts(body: unknown): GoogleContact[] {
  const arr = typeof body === "object" && body !== null ? (body as { contacts?: unknown }).contacts : undefined;
  if (!Array.isArray(arr)) return [];
  const out: GoogleContact[] = [];
  for (const item of arr) {
    if (typeof item !== "object" || item === null) continue;
    const numberE164 = typeof (item as { numberE164?: unknown }).numberE164 === "string" ? (item as { numberE164: string }).numberE164 : "";
    const name = typeof (item as { name?: unknown }).name === "string" ? (item as { name: string }).name : "";
    if (numberE164.startsWith("+")) out.push({ numberE164, name });
  }
  return out;
}

/**
 * Place an outbound reminder call to the owner (bearer-authed). Twilio creds stay
 * in the Worker; the assistant's local push_call.py only holds this URL + secret. Body:
 * { "text": "...", "to"?: E164 }; `to` defaults to USER_CELL_E164. Mirrors the
 * /blocklist + /sync-contacts auth pattern.
 */
async function handlePushCall(request: Request, env: Env): Promise<Response> {
  const secret = env.PUSH_CALL_SECRET;
  if (secret === undefined || secret === "" || request.headers.get("Authorization") !== `Bearer ${secret}`) {
    return new Response("unauthorized", { status: 401 });
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  const textRaw = typeof body === "object" && body !== null ? (body as { text?: unknown }).text : undefined;
  const text = typeof textRaw === "string" ? textRaw.trim() : "";
  if (text === "") {
    return jsonResponse({ ok: false, error: "missing text" }, 400);
  }
  const obj = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const toRaw = obj.to;
  const to = typeof toRaw === "string" && toRaw.startsWith("+") ? toRaw : env.USER_CELL_E164;

  // Escalating call: hand off to the CallEscalation Durable Object, which retries
  // until the owner presses a digit (POST /push-call/ack) or hits the attempt cap.
  if (obj.escalate === true) {
    const escalationId = crypto.randomUUID();
    const base = baseUrlOf(request, env);
    const intervalSec = typeof obj.intervalSec === "number" ? obj.intervalSec : undefined;
    const maxAttempts = typeof obj.maxAttempts === "number" ? obj.maxAttempts : undefined;
    try {
      const stub = env.CALL_ESCALATION.get(env.CALL_ESCALATION.idFromName(escalationId));
      await stub.fetch("https://escalation/start", {
        method: "POST",
        body: JSON.stringify({ id: escalationId, text, to, base, intervalSec, maxAttempts }),
      });
      return jsonResponse({ ok: true, escalationId, to });
    } catch (e) {
      return jsonResponse({ ok: false, error: String(e) }, 502);
    }
  }

  try {
    const sid = await placeCall(env, to, text);
    return jsonResponse({ ok: true, sid, to });
  } catch (e) {
    return jsonResponse({ ok: false, error: String(e) }, 502);
  }
}

/**
 * Twilio `<Gather>` action for an escalating call: the owner pressed a digit. Tell the
 * CallEscalation DO (by the `id` query param) to stop retrying, then thank + hang up.
 * If the gather timed out with no digit, Twilio hangs up without hitting this route,
 * so a non-empty `Digits` is the real ack.
 */
async function handlePushCallAck(request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  const digits = field(form, "Digits");
  const id = new URL(request.url).searchParams.get("id") ?? "";
  if (digits !== "" && id !== "") {
    const stub = env.CALL_ESCALATION.get(env.CALL_ESCALATION.idFromName(id));
    await stub.fetch("https://escalation/ack", { method: "POST" });
    return xml(say("Got it — I'll stop calling. Talk soon.", { hangup: true }));
  }
  return xml(say("Goodbye.", { hangup: true }));
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** After a live-transfer dial ends: hang up if it connected, else roll to voicemail. */
async function handleAfterBridge(request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  if (field(form, "DialCallStatus") === "completed") return xml(hangupResponse());
  const from = field(form, "From");
  const owner = configured(env.OWNER_NAME_SPOKEN) ?? configured(env.OWNER_NAME);
  return xml(
    voicemail({
      prompt:
        owner !== undefined
          ? `${owner} isn't available right now. Please leave a message after the tone.`
          : `No one is available right now. Please leave a message after the tone.`,
      transcribeCallbackUrl: `${baseUrlOf(request, env)}/voicemail?from=${encodeURIComponent(from)}`,
    }),
  );
}

/** Twilio transcription callback for a voicemail: text it to the owner. */
async function handleVoicemail(request: Request, env: Env): Promise<Response> {
  const from = new URL(request.url).searchParams.get("from") ?? "";
  const form = await request.formData();
  const text = field(form, "TranscriptionText");
  const recordingUrl = field(form, "RecordingUrl");
  const who = from !== "" ? from : "Unknown caller";
  const body = `🎙️ Voicemail from ${who}:\n${text !== "" ? text : "(couldn't transcribe — listen via Twilio)"}\n${recordingUrl}`;
  try {
    await sendSms(env, env.USER_CELL_E164, body);
  } catch {
    // best-effort
  }
  return new Response("ok");
}
