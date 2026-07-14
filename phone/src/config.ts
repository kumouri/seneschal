/**
 * Worker environment bindings + helpers to derive runtime settings from them.
 * Secrets come from `.dev.vars` (local) or `wrangler secret put` (deployed);
 * plain config comes from `[vars]` in wrangler.toml.
 */
import type { PostGateAction, ScreenerSettings } from "./screener/decision";
import type { Persona } from "./persona";
import { DEFAULT_PERSONA, greetingLine } from "./persona";

export interface Env {
  // --- bindings ---
  DB: D1Database;
  RELAY_SESSION: DurableObjectNamespace;
  /** Escalating "call me until I answer" reminder loop (one DO instance per escalation id). */
  CALL_ESCALATION: DurableObjectNamespace;

  // --- secrets ---
  ANTHROPIC_API_KEY: string;
  TWILIO_ACCOUNT_SID: string;
  TWILIO_AUTH_TOKEN: string;
  TWILIO_NUMBER_E164: string;
  USER_CELL_E164: string;
  /** Public https base URL of this Worker / tunnel (no trailing slash). */
  PUBLIC_BASE_URL: string;

  // --- vars (wrangler.toml [vars]) ---
  GATE_PROMPT: string;
  POST_GATE_ACTION: string;
  REPUTATION_LOOKUP_ENABLED: string;
  DAILY_BUDGET_USD: string;
  OWNER_NAME?: string;
  /** Phonetic spelling of the owner's name for TTS (e.g. "Shawn" for "Sian"). */
  OWNER_NAME_SPOKEN?: string;
  /** Free-text description of the owner + who to put through vs. block. */
  OWNER_PROFILE?: string;
  /** Optional shared password for owner recognition (a running easter egg until set). */
  OWNER_PASSWORD?: string;
  /** Optional name the assistant introduces itself with. Unset = nameless (the greeting adapts). */
  ASSISTANT_NAME?: string;
  /** Optional ConversationRelay voice id. Unset = the platform's default voice. */
  ASSISTANT_VOICE_ID?: string;
  /** Optional ConversationRelay TTS provider (e.g. "ElevenLabs", "Amazon"). Unset = platform default. */
  ASSISTANT_TTS_PROVIDER?: string;
  /** Bearer secret the Google Contacts sync (Apps Script) must present to POST /sync-contacts. */
  CONTACTS_SYNC_SECRET?: string;
  /** Bearer secret the on-device blocker app must present to GET /blocklist. */
  BLOCKLIST_SYNC_SECRET?: string;
  /** Bearer secret the assistant's local push_call.py must present to POST /push-call. */
  PUSH_CALL_SECRET?: string;
}

/** Trimmed value, or undefined when unset/blank — wrangler.toml [vars] default to "". */
export function configured(v: string | undefined): string | undefined {
  const t = v?.trim();
  return t === undefined || t === "" ? undefined : t;
}

/**
 * The effective persona: env-over-default. ASSISTANT_NAME renames the shipped
 * default (and re-derives its greeting); ASSISTANT_VOICE_ID / ASSISTANT_TTS_PROVIDER
 * pick the voice. Anything unset falls back to DEFAULT_PERSONA.
 */
export function personaFromEnv(env: Env): Persona {
  const name = configured(env.ASSISTANT_NAME) ?? DEFAULT_PERSONA.name;
  return {
    ...DEFAULT_PERSONA,
    name,
    greeting: (owner) => greetingLine(name, owner),
    ttsProvider: configured(env.ASSISTANT_TTS_PROVIDER) ?? DEFAULT_PERSONA.ttsProvider,
    voice: configured(env.ASSISTANT_VOICE_ID) ?? DEFAULT_PERSONA.voice,
  };
}

export function settingsFromEnv(env: Env): ScreenerSettings {
  return {
    userCellE164: env.USER_CELL_E164,
    gatePrompt: env.GATE_PROMPT,
    postGateAction: parsePostGate(env.POST_GATE_ACTION),
    reputationLookupEnabled: env.REPUTATION_LOOKUP_ENABLED === "true",
    dailyBudgetUsd: parseFloatOr(env.DAILY_BUDGET_USD, 0),
  };
}

function parsePostGate(v: string): PostGateAction {
  return v === "ring_through" ? "ring_through" : "converse";
}

function parseFloatOr(v: string, fallback: number): number {
  const n = Number.parseFloat(v);
  return Number.isFinite(n) ? n : fallback;
}
