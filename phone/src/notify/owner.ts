/**
 * The one door every owner notification goes through — verdict summaries,
 * the "pick up!" alert, voicemail transcripts. Picks the channel:
 *
 *   Telegram when it is configured (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID),
 *   SMS (Twilio, to USER_CELL_E164) only as the fallback when it is not.
 *
 * Why Telegram first: US long-code SMS additionally needs Twilio A2P 10DLC
 * registration before it delivers reliably, and Telegram is the channel the
 * assistant's daemon already reaches the owner on. If a configured Telegram
 * send throws, SMS is still tried as a last resort so a transient Bot API
 * error does not silently eat a message. Each send logs which channel carried
 * it, so `wrangler tail` shows the routing.
 */
import type { Env } from "../config";
import { sendSms } from "./sms";
import { sendTelegramMessage, telegramConfigured } from "./telegram";

export type OwnerChannel = "telegram" | "sms";

/** Which channel a notification takes given this env (pure — unit-tested). */
export function ownerChannel(env: Env): OwnerChannel {
  return telegramConfigured(env) ? "telegram" : "sms";
}

/**
 * Deliver `text` to the owner. Returns the channel that carried it. Throws only
 * when every available channel failed — callers treat that as best-effort.
 */
export async function notifyOwner(env: Env, text: string): Promise<OwnerChannel> {
  const preferred = ownerChannel(env);
  if (preferred === "telegram") {
    try {
      await sendTelegramMessage(env, text);
      console.log("notifyOwner: telegram");
      return "telegram";
    } catch (err) {
      console.log(`notifyOwner: telegram failed, falling back to sms: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
  await sendSms(env, env.USER_CELL_E164, text);
  console.log(`notifyOwner: sms${preferred === "telegram" ? " (fallback)" : ""}`);
  return "sms";
}
