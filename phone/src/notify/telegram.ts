/**
 * Notify the owner over Telegram, straight from the Worker to the Bot API.
 *
 * The Worker cannot reach the assistant's resident daemon (it binds 127.0.0.1 on
 * the owner's machine), so this does NOT go through `seneschal/scripts/telegram_send.py`
 * — it speaks to `https://api.telegram.org/bot<TOKEN>/…` directly, and is meant to
 * use the same bot and the same chat id the daemon uses
 * (`seneschal/scripts/TELEGRAM_SETUP.md`). Plain text only: the daemon's
 * Markdown→HTML formatter is Python and there is nothing to format here.
 *
 * Config (Worker secrets, `wrangler secret put`): TELEGRAM_BOT_TOKEN +
 * TELEGRAM_CHAT_ID are required for Telegram to count as configured;
 * TELEGRAM_THREAD_ID is optional and puts every message in one private-chat
 * topic (Bot API `message_thread_id`). Unset ⇒ the main chat.
 *
 * Voicemail audio: Twilio's RecordingUrl is auth-protected, so Telegram cannot
 * fetch it by URL. `sendVoicemailAudio` fetches the `.mp3` with Basic auth
 * (TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN) and streams the body into a multipart
 * `sendAudio` upload. Plain `fetch` so it runs unchanged in the Workers runtime.
 */
import type { Env } from "../config";

const DEFAULT_API_BASE = "https://api.telegram.org";

/** Telegram is configured when both the bot token and the chat id are present. */
export function telegramConfigured(env: Env): boolean {
  return nonEmpty(env.TELEGRAM_BOT_TOKEN) && nonEmpty(env.TELEGRAM_CHAT_ID);
}

/** Bot API method URL (pure — unit-tested). */
export function telegramMethodUrl(token: string, method: string, apiBase = DEFAULT_API_BASE): string {
  return `${apiBase.replace(/\/+$/, "")}/bot${token}/${method}`;
}

/** The Twilio recording as an MP3 URL. Twilio serves `.mp3` / `.wav` by suffix; bare = wav. */
export function recordingMp3Url(recordingUrl: string): string {
  return /\.(mp3|wav)$/i.test(recordingUrl) ? recordingUrl.replace(/\.wav$/i, ".mp3") : `${recordingUrl}.mp3`;
}

function nonEmpty(s: string | undefined): s is string {
  return typeof s === "string" && s.trim() !== "";
}

function apiBase(env: Env): string {
  return nonEmpty(env.TELEGRAM_API_BASE) ? env.TELEGRAM_API_BASE : DEFAULT_API_BASE;
}

/** Common fields every send carries: the chat, and the topic when one is configured. */
function target(env: Env): Record<string, string> {
  const fields: Record<string, string> = { chat_id: env.TELEGRAM_CHAT_ID ?? "" };
  if (nonEmpty(env.TELEGRAM_THREAD_ID)) fields.message_thread_id = env.TELEGRAM_THREAD_ID;
  return fields;
}

async function raiseUnlessOk(method: string, res: Response): Promise<void> {
  if (!res.ok) {
    throw new Error(`Telegram ${method} failed: ${res.status} ${await res.text()}`);
  }
}

/** Send a plain-text message to the configured chat (and topic, if any). */
export async function sendTelegramMessage(env: Env, text: string): Promise<void> {
  if (!telegramConfigured(env)) throw new Error("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)");
  const res = await fetch(telegramMethodUrl(env.TELEGRAM_BOT_TOKEN as string, "sendMessage", apiBase(env)), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...target(env), text, disable_web_page_preview: true }),
  });
  await raiseUnlessOk("sendMessage", res);
}

/**
 * Fetch a Twilio voicemail recording (Basic auth) and upload it to the chat as
 * an audio file via multipart `sendAudio`. Throws on either leg failing; the
 * caller has already sent the text, so a thrown audio is a missing attachment,
 * never a missing message.
 */
export async function sendVoicemailAudio(env: Env, recordingUrl: string, caption: string): Promise<void> {
  if (!telegramConfigured(env)) throw new Error("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)");
  const rec = await fetch(recordingMp3Url(recordingUrl), {
    headers: { Authorization: `Basic ${btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`)}` },
  });
  if (!rec.ok) throw new Error(`Twilio recording fetch failed: ${rec.status}`);
  const audio = await rec.blob();

  const form = new FormData();
  for (const [k, v] of Object.entries(target(env))) form.set(k, v);
  form.set("caption", caption);
  form.set("audio", new Blob([audio], { type: "audio/mpeg" }), "voicemail.mp3");
  const res = await fetch(telegramMethodUrl(env.TELEGRAM_BOT_TOKEN as string, "sendAudio", apiBase(env)), {
    method: "POST",
    body: form,
  });
  await raiseUnlessOk("sendAudio", res);
}
