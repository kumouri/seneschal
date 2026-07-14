/**
 * "Call me until I answer" — an escalating outbound reminder call.
 *
 * `POST /push-call` with `{ escalate: true }` kicks off a `CallEscalation` Durable
 * Object: it places a call (speaking the reminder + "press 1 to stop"), then a
 * `storage.setAlarm()` re-fires the call on a cadence until the owner presses a digit
 * (Twilio POSTs `/push-call/ack`) or the attempt cap is hit. The loop lives here in
 * the Worker so it survives their desktop/daemon being off — the daemon only kicks it
 * off; Cloudflare owns the retries.
 */
import type { Env } from "../config";
import { placeEscalationCall } from "../notify/call";

export const DEFAULT_INTERVAL_SEC = 120; // 2 minutes between callbacks
export const DEFAULT_MAX_ATTEMPTS = 15; // ~30 minutes of trying, then give up

export interface EscalationState {
  attempt: number;
  maxAttempts: number;
  acked: boolean;
}

/**
 * Pure retry decision (unit-tested): stop once the owner has acked or the attempt cap is
 * reached; otherwise place another call. `attempt` is the number of calls already
 * placed.
 */
export function nextEscalationStep(s: EscalationState): "call" | "stop" {
  if (s.acked) return "stop";
  if (s.attempt >= s.maxAttempts) return "stop";
  return "call";
}

interface StartPayload {
  id: string;
  text: string;
  to: string;
  base: string;
  intervalSec?: number;
  maxAttempts?: number;
}

/** Durable Object: one instance per escalation id, driving the retry-until-ack loop. */
export class CallEscalation {
  private state: DurableObjectState;
  private env: Env;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    const { pathname } = new URL(request.url);
    if (pathname === "/start") return this.start(request);
    if (pathname === "/ack") return this.ack();
    return new Response("not found", { status: 404 });
  }

  /** Persist config, place call #1, and arm the first retry alarm. */
  private async start(request: Request): Promise<Response> {
    const p = (await request.json()) as StartPayload;
    const intervalSec = p.intervalSec !== undefined && p.intervalSec > 0 ? p.intervalSec : DEFAULT_INTERVAL_SEC;
    const maxAttempts = p.maxAttempts !== undefined && p.maxAttempts > 0 ? p.maxAttempts : DEFAULT_MAX_ATTEMPTS;
    await this.state.storage.put({
      id: p.id,
      text: p.text,
      to: p.to || this.env.USER_CELL_E164,
      base: p.base || this.env.PUBLIC_BASE_URL,
      intervalSec,
      maxAttempts,
      attempt: 0,
      acked: false,
    });
    await this.placeAndArm();
    return new Response("ok");
  }

  /** The owner pressed a digit — stop the loop. */
  private async ack(): Promise<Response> {
    await this.state.storage.put("acked", true);
    await this.state.storage.deleteAlarm();
    return new Response("ok");
  }

  /** Alarm wake: place the next call if we should still be trying. */
  async alarm(): Promise<void> {
    const acked = (await this.state.storage.get<boolean>("acked")) ?? false;
    const attempt = (await this.state.storage.get<number>("attempt")) ?? 0;
    const maxAttempts = (await this.state.storage.get<number>("maxAttempts")) ?? DEFAULT_MAX_ATTEMPTS;
    if (nextEscalationStep({ attempt, maxAttempts, acked }) === "stop") return;
    await this.placeAndArm();
  }

  private async placeAndArm(): Promise<void> {
    const id = (await this.state.storage.get<string>("id")) ?? "";
    const text = (await this.state.storage.get<string>("text")) ?? "";
    const to = (await this.state.storage.get<string>("to")) ?? this.env.USER_CELL_E164;
    const base = (await this.state.storage.get<string>("base")) ?? this.env.PUBLIC_BASE_URL;
    const intervalSec = (await this.state.storage.get<number>("intervalSec")) ?? DEFAULT_INTERVAL_SEC;
    const attempt = (await this.state.storage.get<number>("attempt")) ?? 0;
    const ackUrl = `${base}/push-call/ack?id=${encodeURIComponent(id)}`;
    try {
      await placeEscalationCall(this.env, to, text, ackUrl);
    } catch (e) {
      // A failed placement must not kill the loop — the next alarm retries.
      console.log(`escalation ${id} attempt ${attempt + 1} failed: ${String(e)}`);
    }
    await this.state.storage.put("attempt", attempt + 1);
    await this.state.storage.setAlarm(Date.now() + intervalSec * 1000);
  }
}
